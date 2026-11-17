"""Typed, approval-first marketplace mutations; no arbitrary provider URL or payload."""
import hashlib
import hmac
import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import DateTime, ForeignKey, String, Text, update, Index, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column
from starlette.concurrency import run_in_threadpool
from .db import Base, get_db
from .models import Product, now
from .config import get_settings
from .marketplace_orders import latest
from .services.mercadolivre import MercadoLivreClient, API_URL
from .security import encrypt, decrypt
from .data_hub import upsert_snapshot, Snapshot
from .operations import audit
from .seller_proceeds import collect_quote
from .auth_guard import limited

router=APIRouter(prefix="/api/v1/listing-manager")
ITEM_ID=re.compile(r"MLB[0-9]+")

class ListingChange(Base):
    __tablename__="marketplace_listing_changes"
    __table_args__=(Index("one_listing_execution_per_seller","seller_id",unique=True,postgresql_where=text("state = 'executing'"),sqlite_where=text("state = 'executing'")),)
    id:Mapped[int]=mapped_column(primary_key=True)
    seller_id:Mapped[str]=mapped_column(String(64),index=True)
    product_id:Mapped[int|None]=mapped_column(ForeignKey("products.id"),nullable=True)
    item_id:Mapped[str|None]=mapped_column(String(64),nullable=True)
    operation:Mapped[str]=mapped_column(String(32))
    state:Mapped[str]=mapped_column(String(32),default="pending")
    payload_encrypted:Mapped[str]=mapped_column(Text)
    review_hash:Mapped[str]=mapped_column(String(64))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    expires_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    decided_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    note:Mapped[str]=mapped_column(String(1000),default="Aguardando sua aprovação.")

class Strict(BaseModel):
    model_config=ConfigDict(extra="forbid",str_strip_whitespace=True)

class Picture(Strict):
    id:str|None=Field(default=None,pattern=r"^[A-Za-z0-9_-]{1,120}$")
    source:str|None=Field(default=None,max_length=2000)
    @model_validator(mode="after")
    def public_source(self):
        if bool(self.id)==bool(self.source):raise ValueError("Informe o identificador ou a URL da foto.")
        if self.source:
            u=urlsplit(self.source);host=(u.hostname or "").lower()
            if u.scheme!="https" or not host or u.username or u.password or u.port not in (None,443) or "." not in host or host.endswith((".local",".internal",".localhost")):
                raise ValueError("Use uma URL HTTPS pública para a imagem.")
            try:
                if not ipaddress.ip_address(host).is_global:raise ValueError("Imagem deve ser pública.")
            except ValueError as e:
                if str(e)=="Imagem deve ser pública.":raise
            if host in {"localhost","metadata.google.internal"}:raise ValueError("Imagem deve ser pública.")
        return self

class Attribute(Strict):
    id:str=Field(pattern=r"^[A-Z0-9_]{1,80}$")
    value_name:str=Field(min_length=1,max_length=500)

class Edit(Strict):
    operation:Literal["price","quantity","status","title","family_name","pictures","description"]
    price:Decimal|None=Field(default=None,gt=0,le=10000000,decimal_places=2)
    quantity:int|None=Field(default=None,ge=0,le=1000000,strict=True)
    status:Literal["active","paused"]|None=None
    title:str|None=Field(default=None,min_length=3,max_length=120)
    family_name:str|None=Field(default=None,min_length=3,max_length=120)
    pictures:list[Picture]|None=Field(default=None,min_length=1,max_length=12)
    description:str|None=Field(default=None,max_length=50000)
    reason:str=Field(min_length=5,max_length=500)
    @model_validator(mode="after")
    def single_change(self):
        fields={"price","quantity","status","title","family_name","pictures","description"}
        if {f for f in fields if getattr(self,f) is not None}!={self.operation}:raise ValueError("Prepare uma alteração por vez.")
        return self

class Publish(Strict):
    title:str=Field(min_length=3,max_length=120)
    category_id:str=Field(pattern=r"^MLB[0-9]+$")
    price:Decimal=Field(gt=0,le=10000000,decimal_places=2)
    quantity:int=Field(gt=0,le=1000000,strict=True)
    condition:Literal["new","used","not_specified"]="new"
    listing_type_id:Literal["gold_special","gold_premium"]="gold_special"
    pictures:list[Picture]=Field(min_length=1,max_length=12)
    attributes:list[Attribute]=Field(default_factory=list,max_length=100)
    sale_terms:list[Attribute]=Field(default_factory=list,max_length=30)
    description:str=Field(min_length=1,max_length=50000)
    free_shipping:bool=True
    reason:str=Field(min_length=5,max_length=500)

class Decision(Strict):
    review_hash:str=Field(pattern=r"^[a-f0-9]{64}$")


def utc(stamp):return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp

def value(row):return json.loads(decrypt(row.payload_encrypted))

def digest(plan):return hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()

def connection(db):
    c=latest(db)
    if not c:raise HTTPException(409,"Conecte sua conta do Mercado Livre.")
    return c

def enabled():
    if not get_settings().marketplace_write_enabled:raise HTTPException(409,"Envio de alterações está desativado nesta instalação.")

def permission_state(db):
    c=latest(db)
    row=db.query(Snapshot).filter_by(seller_id=c.seller_id,module="management_permissions",external_id="application").first() if c else None
    return json.loads(decrypt(row.payload_encrypted)) if row else {"listing_write":"unverified","ads_write":"unverified","notice":"Confira as permissões específicas do aplicativo antes do primeiro envio."}

async def inspect_permissions(db,c,client,headers):
    application_id=get_settings().ml_client_id
    if not re.fullmatch(r"[0-9]+",application_id or ""):
        return {"listing_write":"unverified","ads_write":"unverified","notice":"Identificador do aplicativo não configurado para conferir as permissões."}
    data=await provider(client,headers,"GET","/applications/"+application_id)
    scopes=data.get("scopes")
    def access(feature):
        if not isinstance(scopes,list):return "unverified"
        relevant=[s for s in scopes if isinstance(s,str) and (":"+feature+":") in s]
        if any(s.endswith("/read-write") for s in relevant):return "configured"
        if relevant:return "read_only"
        return "unverified"
    info={"listing_write":access("publish-sync"),"ads_write":access("ads"),"checked_at":now().isoformat(),
        "notice":"A permissão geral write não substitui permissões específicas. No DevCenter, libere escrita em Publicação e sincronização; para publicidade, libere o recurso correspondente. Depois salve, renove a autorização em Ajustes e confira novamente. A permissão configurada não garante disponibilidade de todos os recursos para a conta."}
    upsert_snapshot(db,c.seller_id,"management_permissions","application","/applications/configured",info);db.commit();return info

@router.post("/permissions/check")
async def check_permissions(request:Request,db:Session=Depends(get_db)):
    await throttle(request);c=connection(db);headers=await MercadoLivreClient(db).authorization_headers(c.id)
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:return await inspect_permissions(db,c,client,headers)
    except (ProviderFailure,httpx.HTTPError,ValueError):raise HTTPException(502,"Não foi possível conferir as permissões do aplicativo.") from None

class ProviderFailure(Exception):
    def __init__(self,status,codes=()):self.status=status;self.codes=codes
    def detail(self):
        prefix="Mercado Livre não autorizou a operação. Confira as permissões do aplicativo e renove a conexão." if self.status in (401,403) else "Mercado Livre recusou a operação."
        return prefix+f" HTTP {self.status}."+(" Códigos: "+", ".join(self.codes) if self.codes else "")

async def provider(client,headers,method,path,body=None,params=None,absent=False):
    # All paths originate exclusively in the typed operations below.
    r=await client.request(method,API_URL+path,headers=headers,json=body,params=params)
    if absent and r.status_code==404:return None
    if r.status_code>=300:
        codes=[]
        try:
            p=r.json()
            for cause in p.get("cause",[]):
                code=cause.get("code","")
                if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}",str(code)):codes.append(code)
        except (ValueError,AttributeError,TypeError):pass
        raise ProviderFailure(r.status_code,codes[:8])
    if r.status_code==204 or not r.content:return {}
    return r.json()

async def own_item(client,headers,c,identity):
    if not ITEM_ID.fullmatch(identity or ""):raise HTTPException(409,"Anúncio brasileiro inválido.")
    item=await provider(client,headers,"GET","/items/"+identity)
    if not isinstance(item,dict) or item.get("id")!=identity or str(item.get("seller_id"))!=c.seller_id:
        raise HTTPException(403,"Anúncio não pertence à conta conectada.")
    return item

async def account(client,headers,c):
    user=await provider(client,headers,"GET","/users/me")
    if str(user.get("id"))!=c.seller_id:raise HTTPException(403,"Conta da autorização não confere.")
    return user

def capabilities(item,user):
    active=item.get("status")=="active";editable=item.get("status") in {"active","paused"}
    catalog=bool(item.get("catalog_listing"));up=bool(item.get("family_name") or "user_product_listing" in item.get("tags",[]))
    multi="warehouse_management" in user.get("tags",[])
    stock=editable and not item.get("variations") and not multi and (item.get("shipping") or {}).get("logistic_type")!="fulfillment" and not item.get("inventory_id")
    return {"price":editable and not item.get("variations") and "dynamic_standard_price" not in item.get("tags",[]),
            "quantity":stock,"status":editable,"title":active and not catalog and not up and item.get("sold_quantity")==0,
            "family_name":active and not catalog and up,"pictures":active and not catalog and not item.get("variations"),
            "description":active and not catalog}

def public_item(item,user):
    fields=("id","title","family_name","price","currency_id","available_quantity","status","sold_quantity","catalog_listing","user_product_id","category_id")
    return {**{k:item.get(k) for k in fields},"pictures":[{"id":p.get("id")} for p in item.get("pictures",[])],
            "capabilities":capabilities(item,user),"shared_changes":bool(item.get("user_product_id") or item.get("item_relations")),
            "notice":"Catálogo controla conteúdo e fotos da ficha. Estoque e outros campos compartilhados podem refletir em anúncios vinculados. Full, multi-origem e variações exigem gestão específica."}

def snapshot(db,c,item):
    upsert_snapshot(db,c.seller_id,"listings",item["id"],"/items/"+item["id"],item)
    p=db.query(Product).filter_by(sku="meli:"+item["id"]).first()
    if p:
        p.name=item.get("title") or p.name
        if item.get("price") is not None:p.reference_price=float(item["price"])
        if item.get("available_quantity") is not None:p.stock=int(item["available_quantity"])
    return p

async def before_state(client,headers,c,item,operation,user):
    allowed=capabilities(item,user)
    if not allowed.get(operation):raise HTTPException(409,"Esta alteração não está disponível para este anúncio. Confira catálogo, vendas, logística, variações ou preço automático.")
    if operation=="price":
        auto=await provider(client,headers,"GET",f"/pricing-automation/items/{item['id']}/automation",absent=True)
        if auto and auto.get("status") in {"ACTIVE","PAUSED"}:raise HTTPException(409,"O anúncio tem uma regra de preço do Mercado Livre. Gerencie essa regra antes de alterar manualmente.")
    field={"quantity":"available_quantity","description":"plain_text"}.get(operation,operation)
    description_exists=False
    if operation=="description":
        desc=await provider(client,headers,"GET",f"/items/{item['id']}/description",absent=True)
        description_exists=desc is not None
        current=desc.get("plain_text","") if desc else ""
    elif operation=="pictures":current=[{"id":p["id"]} for p in item.get("pictures",[]) if p.get("id")]
    else:current=item.get(field)
    # Shared topology, promotions and sale status are relevant to the approval.
    return {"value":current,"description_exists":description_exists,"status":item.get("status"),"currency":item.get("currency_id"),"sold_quantity":item.get("sold_quantity"),
            "user_product_id":item.get("user_product_id"),"item_relations":item.get("item_relations",[]),"catalog_listing":item.get("catalog_listing"),
            "tags":sorted(item.get("tags",[])),"shipping":item.get("shipping"),"variations":item.get("variations",[])}

async def margin(db,product_id,price):
    quote=await collect_quote(db,product_id,Decimal(str(price)))
    if quote["state"]!="estimated" or quote["minimum_margin"] is None:raise HTTPException(409,"Atualize os custos da API e defina a meta para conferir a margem deste preço.")
    if not quote["meets_target"]:raise HTTPException(409,"Preço abaixo da sua margem mínima. Prepare outro preço ou revise sua meta conscientemente.")
    return {k:quote[k] for k in ("price","sale_fee","seller_shipping_estimate","remaining_estimate","remaining_margin_percent","minimum_margin")}

def plan_out(row):
    p=value(row)
    return {"id":row.id,"item_id":row.item_id,"operation":row.operation,"state":row.state,"review_hash":row.review_hash,
            "before":p.get("before",{}).get("value"),"after":p["body"],"description":p.get("description"),"reason":p["reason"],
            "margin":p.get("margin"),"notice":p.get("notice"),"created_at":row.created_at,"expires_at":row.expires_at,"note":row.note}

def save_plan(db,c,product_id,item_id,operation,plan):
    row=ListingChange(seller_id=c.seller_id,product_id=product_id,item_id=item_id,operation=operation,
        payload_encrypted=encrypt(json.dumps(plan,ensure_ascii=False)),review_hash=digest(plan),expires_at=now()+timedelta(minutes=15))
    db.add(row);db.flush();audit(db,"listing_change.prepared","listing_change",row.id,{"operation":operation,"item_id":item_id})
    db.commit();db.refresh(row);return plan_out(row)

async def throttle(request):
    if await run_in_threadpool(limited,request,"listing_manager"):raise HTTPException(429,"Aguarde um minuto antes de novas operações.")

def scoped_plan(db,identity):
    row=db.get(ListingChange,identity)
    if not row or row.seller_id!=connection(db).seller_id:raise HTTPException(404,"Alteração não encontrada nesta conta.")
    return row

@router.get("/status")
def status(db:Session=Depends(get_db)):
    c=latest(db)
    return {"write_enabled":get_settings().marketplace_write_enabled,"permissions":permission_state(db),"connected":bool(c),"approval_required":True,
        "listing_actions":["price","quantity","status","title","family_name","pictures","description","publish"],
        "ads_state":"requires_provider_access","ads_notice":"A consulta atual de publicidade não disponibilizou anunciante. Criação e otimização de campanhas pagas ainda não estão liberadas nesta integração.",
        "automation_notice":"IA e testes automáticos de criativos continuam desativados. Nenhuma proposta ou campanha simulada antiga executa mudanças reais."}

@router.get("/plans")
def plans(db:Session=Depends(get_db)):
    c=latest(db)
    return [plan_out(r) for r in db.query(ListingChange).filter_by(seller_id=c.seller_id).order_by(ListingChange.id.desc()).limit(50)] if c else []

@router.get("/products/{product_id}")
async def get_item(product_id:int,db:Session=Depends(get_db)):
    p=db.get(Product,product_id);c=connection(db)
    if not p or not p.sku.startswith("meli:"):raise HTTPException(409,"Selecione um anúncio importado.")
    headers=await MercadoLivreClient(db).authorization_headers(c.id)
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            item=await own_item(client,headers,c,p.sku[5:]);user=await account(client,headers,c)
            result=public_item(item,user)
            if result["capabilities"]["description"]:
                d=await provider(client,headers,"GET",f"/items/{item['id']}/description",absent=True)
                result["description"]=(d or {}).get("plain_text","")
            snapshot(db,c,item);db.commit();return result
    except ProviderFailure as e:raise HTTPException(502,e.detail()) from None
    except (httpx.HTTPError,ValueError):raise HTTPException(502,"Não foi possível consultar o anúncio.") from None

@router.post("/products/{product_id}/plans")
async def prepare(product_id:int,body:Edit,request:Request,db:Session=Depends(get_db)):
    await throttle(request)
    p=db.get(Product,product_id);c=connection(db)
    if not p or not p.sku.startswith("meli:"):raise HTTPException(409,"Selecione um anúncio importado.")
    headers=await MercadoLivreClient(db).authorization_headers(c.id)
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            item=await own_item(client,headers,c,p.sku[5:]);user=await account(client,headers,c)
            before=await before_state(client,headers,c,item,body.operation,user)
            field={"quantity":"available_quantity","description":"plain_text"}.get(body.operation,body.operation)
            desired=body.model_dump(mode="json",exclude_none=True)[body.operation]
            if body.operation=="price":desired=float(body.price)
            if desired==before["value"]:raise HTTPException(409,"O valor já está assim no Mercado Livre.")
            if body.operation=="title":
                category=await provider(client,headers,"GET","/categories/"+item["category_id"])
                if len(desired)>category.get("settings",{}).get("max_title_length",60):raise HTTPException(422,"Título excede o limite da categoria.")
            plan={"body":{field:desired},"before":before,"reason":body.reason,
                  "notice":"Altera o anúncio real após sua aprovação. Campos compartilhados podem atualizar outros anúncios vinculados ao mesmo produto. Estoque zero pode pausar o anúncio."}
            if body.operation=="price":plan["margin"]=await margin(db,product_id,desired)
            return save_plan(db,c,product_id,item["id"],body.operation,plan)
    except ProviderFailure as e:raise HTTPException(502,e.detail()) from None
    except (httpx.HTTPError,ValueError):raise HTTPException(502,"Não foi possível preparar a alteração.") from None

@router.get("/categories/search")
async def category_search(q:str,db:Session=Depends(get_db)):
    if not 3<=len(q)<=120:raise HTTPException(422,"Informe de 3 a 120 caracteres.")
    c=connection(db);headers=await MercadoLivreClient(db).authorization_headers(c.id)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            rows=await provider(client,headers,"GET","/sites/MLB/domain_discovery/search",params={"q":q,"limit":5})
            return [{k:r.get(k) for k in ("category_id","category_name","domain_name")} for r in rows]
    except (ProviderFailure,httpx.HTTPError,ValueError):raise HTTPException(502,"Não foi possível sugerir categorias.") from None

@router.get("/categories/{category_id}")
async def category_details(category_id:str,db:Session=Depends(get_db)):
    if not ITEM_ID.fullmatch(category_id):raise HTTPException(422,"Categoria inválida.")
    c=connection(db);headers=await MercadoLivreClient(db).authorization_headers(c.id)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            category=await provider(client,headers,"GET","/categories/"+category_id)
            attrs=await provider(client,headers,"GET","/categories/"+category_id+"/attributes")
            return {"name":category.get("name"),"settings":category.get("settings"),"attributes":[{"id":a["id"],"name":a.get("name"),"required":bool(a.get("tags",{}).get("required")),"values":[v.get("name") for v in a.get("values",[])][:30]} for a in attrs if not a.get("tags",{}).get("read_only")]}
    except (ProviderFailure,httpx.HTTPError,ValueError):raise HTTPException(502,"Não foi possível consultar a categoria.") from None

@router.post("/publish/plans")
async def prepare_publish(body:Publish,request:Request,db:Session=Depends(get_db)):
    await throttle(request);c=connection(db);headers=await MercadoLivreClient(db).authorization_headers(c.id)
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            user=await account(client,headers,c)
            if not user.get("status",{}).get("list",{}).get("allow"):raise HTTPException(409,"A conta não está liberada para publicar.")
            data=body.model_dump(mode="json",exclude_none=True)
            content={k:data[k] for k in ("category_id","condition","listing_type_id","pictures","attributes","sale_terms")}
            field="family_name" if "user_product_seller" in user.get("tags",[]) else "title"
            content.update({field:body.title,"price":float(body.price),"available_quantity":body.quantity,"currency_id":"BRL","buying_mode":"buy_it_now","site_id":"MLB","shipping":{"mode":"me2","free_shipping":body.free_shipping}})
            await provider(client,headers,"POST","/items/validate",content)
            plan={"body":content,"description":body.description,"reason":body.reason,"account_tags":sorted(user.get("tags",[])),
                  "notice":"Publicará um anúncio real e poderá gerar tarifas conforme o tipo de publicação. A descrição é enviada depois da criação. Se essa segunda etapa falhar, o anúncio criado continuará identificado aqui e não será publicado novamente."}
            return save_plan(db,c,None,None,"publish",plan)
    except ProviderFailure as e:raise HTTPException(422,e.detail()) from None
    except (httpx.HTTPError,ValueError):raise HTTPException(502,"Não foi possível validar a publicação.") from None


def finish(db,row,state,note):
    row.state=state;row.note=note;audit(db,"listing_change."+state,"listing_change",row.id,{"item_id":row.item_id,"operation":row.operation});db.commit();db.refresh(row);return plan_out(row)

@router.post("/plans/{identity}/reject")
def reject(identity:int,body:Decision,db:Session=Depends(get_db)):
    row=scoped_plan(db,identity)
    if not hmac.compare_digest(row.review_hash,body.review_hash):raise HTTPException(409,"Revise novamente esta alteração.")
    changed=db.execute(update(ListingChange).where(ListingChange.id==identity,ListingChange.state=="pending").values(state="rejected",decided_at=now(),note="Rejeitada por você; nada foi enviado."))
    if changed.rowcount!=1:db.rollback();raise HTTPException(409,"Esta proposta já foi decidida.")
    audit(db,"listing_change.rejected","listing_change",row.id,{});db.commit();db.refresh(row);return plan_out(row)

@router.post("/plans/{identity}/execute")
async def execute(identity:int,body:Decision,request:Request,db:Session=Depends(get_db)):
    enabled();await throttle(request);row=scoped_plan(db,identity)
    if not hmac.compare_digest(row.review_hash,body.review_hash):raise HTTPException(409,"Revise o conteúdo antes de aprovar.")
    if row.state!="pending":raise HTTPException(409,"Esta proposta já foi decidida. Consulte o resultado; não repita o envio.")
    if utc(row.expires_at)<=now():return finish(db,row,"expired","A proposta expirou. Prepare uma nova com os dados atuais.")
    c=connection(db);plan=value(row);headers=await MercadoLivreClient(db).authorization_headers(c.id)
    # Serialize external changes per seller, including preflight, across processes.
    try:
        changed=db.execute(update(ListingChange).where(ListingChange.id==identity,ListingChange.state=="pending").values(state="executing",decided_at=now(),note="Aprovada por você; conferindo dados atuais."))
        if changed.rowcount!=1:db.rollback();raise HTTPException(409,"Outra execução já decidiu esta proposta.")
        audit(db,"listing_change.approved","listing_change",row.id,{"review_hash":row.review_hash});db.commit();db.refresh(row)
    except IntegrityError:
        db.rollback();raise HTTPException(409,"Há outra alteração sendo executada nesta conta. Confira o resultado dela primeiro.") from None
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            permissions=await inspect_permissions(db,c,client,headers)
            if permissions["listing_write"]=="read_only":return finish(db,row,"failed","O aplicativo está com publicação/sincronização somente leitura. Libere escrita no DevCenter e renove a autorização; nenhum anúncio foi alterado.")
            user=await account(client,headers,c)
            if row.operation=="publish":
                if sorted(user.get("tags",[]))!=plan["account_tags"]:return finish(db,row,"conflict","A configuração da conta mudou. Prepare novamente.")
                await provider(client,headers,"POST","/items/validate",plan["body"])
            else:
                item=await own_item(client,headers,c,row.item_id)
                fresh=await before_state(client,headers,c,item,row.operation,user)
                if fresh!=plan["before"]:return finish(db,row,"conflict","O anúncio mudou desde a revisão. Prepare uma nova proposta.")
                if row.operation=="price":
                    fresh_margin=await margin(db,row.product_id,plan["body"]["price"])
                    if fresh_margin!=plan.get("margin"):return finish(db,row,"conflict","As tarifas ou a meta mudaram. Revise uma nova proposta.")
            enabled()
            if utc(row.expires_at)<=now():return finish(db,row,"expired","A proposta expirou durante a conferência; prepare uma nova.")
            try:
                if row.operation=="publish":
                    item=await provider(client,headers,"POST","/items",plan["body"])
                    if not isinstance(item,dict) or not ITEM_ID.fullmatch(str(item.get("id",""))) or str(item.get("seller_id"))!=c.seller_id:
                        return finish(db,row,"unknown","Resposta de criação inconclusiva. Confira seus anúncios antes de criar outra publicação.")
                    row.item_id=item["id"];db.commit()
                    snapshot(db,c,item)
                    if not db.query(Product).filter_by(sku="meli:"+item["id"]).first():
                        p=Product(sku="meli:"+item["id"],name=item.get("title") or plan["body"].get("family_name") or plan["body"].get("title"),brand="Mercado Livre",reference_price=float(item.get("price",plan["body"]["price"])),stock=int(item.get("available_quantity",plan["body"]["available_quantity"])))
                        db.add(p);db.flush();row.product_id=p.id
                    db.commit()
                    try:await provider(client,headers,"POST",f"/items/{row.item_id}/description",{"plain_text":plan["description"]})
                    except (ProviderFailure,httpx.HTTPError,ValueError):return finish(db,row,"partial","Anúncio criado; descrição não confirmada. Consulte o anúncio e prepare apenas a descrição, sem republicar.")
                else:
                    path=f"/items/{row.item_id}"+("/description" if row.operation=="description" else "")
                    method="POST" if row.operation=="description" and not plan["before"].get("description_exists") else "PUT"
                    await provider(client,headers,method,path,plan["body"])
                return await verify(db,row,c,client,headers)
            except ProviderFailure as e:
                return finish(db,row,"unknown" if e.status>=500 else "failed",e.detail()+" Não reenviado automaticamente.")
            except (httpx.HTTPError,ValueError):return finish(db,row,"unknown","Sem confirmação do Mercado Livre. Use Conferir resultado antes de qualquer nova alteração.")
    except HTTPException as e:
        return finish(db,row,"conflict",str(e.detail)+" Nenhuma alteração foi enviada.")
    except ProviderFailure as e:return finish(db,row,"failed",e.detail()+" A conferência prévia falhou; nenhuma alteração foi enviada.")
    except (httpx.HTTPError,ValueError):return finish(db,row,"failed","Consulta prévia falhou. Nenhuma alteração foi enviada.")

async def verify(db,row,c,client,headers):
    plan=value(row)
    if not row.item_id:return plan_out(row)
    item=await own_item(client,headers,c,row.item_id);snapshot(db,c,item)
    if row.operation=="publish":
        desc=await provider(client,headers,"GET",f"/items/{row.item_id}/description",absent=True)
        matches=(desc or {}).get("plain_text")==plan["description"]
        return finish(db,row,"succeeded" if matches else "partial","Anúncio criado e descrição conferida." if matches else "Anúncio criado. Descrição ainda não confirmada; não republique.")
    if row.operation=="description":
        desc=await provider(client,headers,"GET",f"/items/{row.item_id}/description",absent=True);actual={"plain_text":(desc or {}).get("plain_text","")}
    elif row.operation=="pictures":
        current=[p.get("id") for p in item.get("pictures",[])]
        # New image URLs get new IDs. Do not pretend a 200 proves that the intended image is visible.
        matches=all(p.get("id") for p in plan["body"]["pictures"]) and current==[p["id"] for p in plan["body"]["pictures"]]
        return finish(db,row,"succeeded" if matches else "sent","Fotos conferidas." if matches else "Envio registrado. Fotos podem estar em processamento; confira a galeria no Mercado Livre.")
    else:actual={field:item.get(field) for field in plan["body"]}
    matches=actual==plan["body"]
    return finish(db,row,"succeeded" if matches else "sent","Alteração conferida no Mercado Livre." if matches else "Envio registrado; o valor retornado ainda difere da proposta. Confira novamente.")

@router.post("/plans/{identity}/verify")
async def reconcile(identity:int,request:Request,db:Session=Depends(get_db)):
    await throttle(request);row=scoped_plan(db,identity)
    if row.state not in {"executing","unknown","sent","partial","succeeded"}:raise HTTPException(409,"Esta proposta não tem envio para conferir.")
    if row.state=="executing" and row.decided_at and now()-utc(row.decided_at)<timedelta(minutes=5):
        raise HTTPException(409,"A execução ainda está em andamento. Aguarde a conclusão antes de conferir.")
    if not row.item_id:
        return finish(db,row,"unknown","Criação sem identificador confirmado. Consulte os anúncios da conta antes de preparar outra publicação. Não houve reenvio.")
    c=connection(db);headers=await MercadoLivreClient(db).authorization_headers(c.id)
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:return await verify(db,row,c,client,headers)
    except (ProviderFailure,httpx.HTTPError,ValueError):raise HTTPException(502,"Não foi possível conferir o resultado. Nenhum envio foi repetido.") from None
