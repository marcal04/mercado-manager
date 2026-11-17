"""Authenticated read-only operational mirror. Provider limits are surfaced, never hidden."""
import asyncio
import time
import json
import hashlib
from datetime import datetime,timedelta,timezone
from urllib.parse import quote
import httpx
from fastapi import APIRouter,Depends,HTTPException,BackgroundTasks,Query
from sqlalchemy import String,Text,JSON,DateTime,UniqueConstraint,Index,text,func
from sqlalchemy.orm import Mapped,mapped_column,Session
from sqlalchemy.exc import IntegrityError
from .config import get_settings
from .db import Base,get_db,SessionLocal
from .models import Product,AuditEvent,now
from .security import encrypt,decrypt
from .marketplace_orders import latest,ImportedOrder,SyncRun,normalize
from .services.mercadolivre import MercadoLivreClient,API_URL

BILLING_GAP_SECONDS=6
MODULES={"account":"Conta e reputação","listings":"Anúncios e variações","descriptions":"Descrições","prices":"Preços e condições","fees":"Tarifas de venda estimadas","shipping_quotes":"Cotações de envio","visits":"Visitas e métricas","questions":"Perguntas e respostas","orders":"Pedidos e taxas realizadas","shipments":"Envios e rastreamento","shipment_costs":"Custos efetivos de envio","messages":"Mensagens pós-venda","claims":"Reclamações","returns":"Devoluções e cancelamentos","promotions":"Promoções disponíveis","full_stock":"Estoque Full","categories":"Categorias e atributos","billing":"Faturamento e documentos","ads":"Publicidade e campanhas","ads_metrics":"Métricas de publicidade","financial":"Descontos e leitura financeira"}
router=APIRouter(prefix="/api/v1/data-hub")
class Snapshot(Base):
    __tablename__="api_snapshots"
    __table_args__=(UniqueConstraint("seller_id","module","external_id"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    seller_id:Mapped[str]=mapped_column(String(64),index=True)
    module:Mapped[str]=mapped_column(String(40),index=True)
    external_id:Mapped[str]=mapped_column(String(200))
    source:Mapped[str]=mapped_column(String(300))
    fetched_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    payload_encrypted:Mapped[str]=mapped_column(Text)
class DataJob(Base):
    __tablename__="api_data_jobs"
    __table_args__=(Index("one_active_data_job","seller_id",unique=True,postgresql_where=text("status = 'running'"),sqlite_where=text("status = 'running'")),)
    id:Mapped[int]=mapped_column(primary_key=True)
    seller_id:Mapped[str]=mapped_column(String(64),index=True)
    status:Mapped[str]=mapped_column(String(32))
    started_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    finished_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    progress:Mapped[dict]=mapped_column(JSON,default=dict)

def clean(data):
    secrets={"access_token","refresh_token","client_secret","password","authorization","secure_key","card_number","security_code"}
    if isinstance(data,dict):return {k:clean(v) for k,v in data.items() if k.lower() not in secrets}
    if isinstance(data,list):return [clean(v) for v in data]
    return data

def upsert_snapshot(db,seller,module,identifier,source,payload):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sq_insert
    insert=pg_insert if db.bind.dialect.name=="postgresql" else sq_insert
    stmt=insert(Snapshot).values(seller_id=seller,module=module,external_id=str(identifier)[:200],source=source[:300],fetched_at=now(),payload_encrypted=encrypt(json.dumps(clean(payload),ensure_ascii=False)))
    db.execute(stmt.on_conflict_do_update(index_elements=["seller_id","module","external_id"],set_={k:stmt.excluded[k] for k in ("source","fetched_at","payload_encrypted")}))

class ProviderError(Exception):
    def __init__(self,code):self.code=code
class Hub:
    def __init__(self,db,job,headers,client):
        self.db,self.job,self.headers,self.client=db,job,headers,client
        self.seller=job.seller_id;self.items=[];self.orders=[];self.claims=[];self.module="";self.errors=[];self.identifiers=set();self.partial=False;self.calls=0;self.last_billing=0
    def check(self):
        self.db.refresh(self.job)
        if self.job.progress.get("_cancel"):raise asyncio.CancelledError()
    async def get(self,path,params=None,headers=None):
        self.check();self.calls+=1
        if self.calls>20000:self.partial=True;raise ProviderError(998)
        if not path.startswith('/') or path.startswith('//') or '?' in path or '..' in path:raise ValueError("Invalid fixed provider path")
        for attempt in range(3):
            if path.startswith('/billing/'):
                delay=BILLING_GAP_SECONDS-(time.monotonic()-self.last_billing)
                if delay>0:await asyncio.sleep(delay)
                self.last_billing=time.monotonic()
            r=await self.client.get(API_URL+path,params=params,headers={**self.headers,**(headers or {})})
            if r.status_code==206:self.partial=True
            code=r.status_code
            if code>=400 and path.startswith('/billing/'):
                try:
                    if 'code 429' in str(r.json().get('message','')):code=429
                except (ValueError,AttributeError):pass
            if code==429 and attempt<2:
                await asyncio.sleep(min(30,max(2*(attempt+1),int(r.headers.get("retry-after","0")) if r.headers.get("retry-after","0").isdigit() else 0)));continue
            if code>=400:raise ProviderError(code)
            return r.json()
    def save(self,key,path,payload):
        upsert_snapshot(self.db,self.seller,self.module,key,path,payload);self.identifiers.add(str(key));self.db.commit()
    async def capture(self,path,key=None,params=None,headers=None,quote_context=None):
        try:
            payload=await self.get(path,params,headers)
            if quote_context is not None:
                from .seller_proceeds import annotate
                payload=annotate(payload,quote_context)
            if path=='/users/me' and str(payload.get('id'))!=self.seller:raise ValueError('Foreign account')
            if path.startswith('/items/') and path.count('/')==2 and isinstance(payload,dict) and str(payload.get('seller_id'))!=self.seller:raise ValueError('Foreign item')
            if path.startswith('/orders/') and path.count('/')==2 and isinstance(payload,dict) and str(payload.get('seller',{}).get('id'))!=self.seller:raise ValueError('Foreign order')
            self.save(key or path,path,payload);return payload
        except (ProviderError,httpx.HTTPError,ValueError) as e:
            self.errors.append({"source":path,"http":getattr(e,"code",None),"issue":"provider" if isinstance(e,ProviderError) else "network_or_format"});return None
    async def pages(self,path,params=None,keys=("results",),limit=50,headers=None,offset_limit=10000):
        offset=0;seen=set();rows=[]
        while offset<offset_limit:
            try:p=await self.get(path,{**(params or {}),"offset":offset,"limit":limit},headers)
            except (ProviderError,httpx.HTTPError,ValueError) as e:
                self.errors.append({"source":path,"http":getattr(e,"code",None),"issue":"pagination"});self.partial=bool(rows);break
            batch=p if isinstance(p,list) else next((p[k] for k in keys if isinstance(p.get(k),list)),None)
            if batch is None:self.errors.append({"source":path,"http":None,"issue":"unexpected_shape"});break
            previous_count=len(seen)
            for i,item in enumerate(batch):
                if self.module=='claims' and isinstance(item,dict):
                    roles=[x.get('role') for x in item.get('players',[]) if str(x.get('user_id'))==self.seller]
                    if not roles:raise ValueError('Claim is not linked to the connected account')
                    item={**item,'_integration_account_roles':roles}
                if self.module=='questions' and isinstance(item,dict) and item.get('seller_id') is not None and str(item['seller_id'])!=self.seller:raise ValueError('Foreign question')
                key=str(item.get("id",item.get("key",item.get("advertiser_id",item.get("ad_group_id",item.get("campaign_id",f"{offset+i}")))))) if isinstance(item,dict) else str(item)
                if key not in seen:
                    identity=key if self.module in ('questions','claims','messages') else hashlib.sha256((path+json.dumps(params or {},sort_keys=True)).encode()).hexdigest()[:10]+":"+key
                    self.save(identity,path,item);rows.append(item);seen.add(key)
            paging=p.get("paging",{}) if isinstance(p,dict) else {}
            total=paging.get("total",p.get("total") if isinstance(p,dict) else None)
            if not batch or (total is not None and offset+len(batch)>=int(total)):break
            if len(batch)<limit and total is None:break
            if batch and len(seen)==previous_count:self.partial=True;break
            offset+=len(batch)
        else:self.partial=True
        return rows
    async def run_account(self):
        await self.capture('/users/me',self.seller)
    async def run_listings(self):
        from .catalog_sync import synchronize
        result=await synchronize(self.db,self.seller,self.get)
        self.items=result["items"];self.partial=result["partial"] and (bool(self.items) or not result["errors"]);self.errors.extend(result["errors"])
        self.identifiers.update(i["id"] for i in self.items)
        self.items=[i for i in self.items if i.get("status") not in {"closed","deleted"} and "deleted" not in (i.get("sub_status") or [])]
    async def run_descriptions(self):
        from .api_completion import collect_descriptions
        await collect_descriptions(self)
    async def run_prices(self):
        for i in self.items:await self.capture(f"/items/{i['id']}/prices",i['id'])
    async def run_fees(self):
        from .seller_proceeds import fee_params,context
        from decimal import Decimal
        for i in self.items:
            price=Decimal(str(i.get('price',0)))
            await self.capture(f"/sites/{i.get('site_id','MLB')}/listing_prices",i['id'],fee_params(i,price),quote_context=context(i,price))
    async def run_shipping_quotes(self):
        from .seller_proceeds import shipping_params,context
        from decimal import Decimal
        for i in self.items:
            price=Decimal(str(i.get('price',0)))
            await self.capture(f"/users/{self.seller}/shipping_options/free",i['id'],shipping_params(i,price),quote_context=context(i,price))
    async def run_visits(self):
        for i in self.items:await self.capture(f"/items/{i['id']}/visits/time_window",i['id'],{"last":90,"unit":"day"})
        await self.capture(f"/users/{self.seller}/items_visits/time_window",self.seller,{"last":90,"unit":"day"})
    async def run_questions(self):
        await self.pages('/questions/search',{"seller_id":self.seller,"api_version":4},keys=("questions",))
    async def run_orders(self):
        end=now();start=end-timedelta(days=365);data={}
        async def window(a,b,depth=0):
            params={"seller":self.seller,"sort":"date_desc","limit":50,"offset":0,"order.date_created.from":a.isoformat(timespec='milliseconds'),"order.date_created.to":b.isoformat(timespec='milliseconds')}
            p=await self.get('/orders/search',params);total=int(p['paging']['total'])
            if total>1000 and depth<20 and b-a>timedelta(seconds=1):
                mid=a+(b-a)/2;await window(a,mid,depth+1);await window(mid,b,depth+1);return
            offset=0
            while True:
                batch=p['results']
                for o in batch:
                    if str(o.get('seller',{}).get('id'))!=self.seller:raise ValueError('Foreign order')
                    data[str(o['id'])]=o
                offset+=len(batch)
                if offset>=total or not batch:break
                if offset>=1000:self.partial=True;break
                p=await self.get('/orders/search',{**params,"offset":offset})
        await window(start,end)
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sq_insert
        insert=pg_insert if self.db.bind.dialect.name=='postgresql' else sq_insert
        for id,o in data.items():
            detail=await self.capture('/orders/'+id,id)
            if detail and str(detail.get('seller',{}).get('id'))!=self.seller:raise ValueError('Foreign order')
            if not detail:self.save(id,'/orders/search',o)
            value=detail or o;self.orders.append(value)
            normalized=normalize(value,self.seller);stmt=insert(ImportedOrder).values(**normalized)
            self.db.execute(stmt.on_conflict_do_update(index_elements=['seller_id','external_id'],set_={k:stmt.excluded[k] for k in normalized if k not in ('seller_id','external_id')},where=stmt.excluded.updated_at>=ImportedOrder.updated_at));self.db.commit()
        self.db.add(SyncRun(seller_id=self.seller,status='partial' if self.partial or self.errors else 'success',days=365,count=len(data),started_at=start,finished_at=now(),note='Central de dados: histórico consultado de até 365 dias.'));self.db.commit()
    async def run_shipments(self):
        for id in sorted({str(o['shipping']['id']) for o in self.orders if (o.get('shipping') or {}).get('id')}):await self.capture('/shipments/'+id,id)
    async def run_shipment_costs(self):
        for id in sorted({str(o['shipping']['id']) for o in self.orders if (o.get('shipping') or {}).get('id')}):await self.capture('/shipments/'+id+'/costs',id)
    async def run_messages(self):
        for id in sorted({str(o.get('pack_id') or o['id']) for o in self.orders}):
            await self.pages(f'/messages/packs/{id}/sellers/{self.seller}',{"tag":"post_sale","mark_as_read":"false"},keys=("messages",))
    async def run_claims(self):
        all_claims={}
        for status in ('opened','closed'):
            batch=await self.pages('/post-purchase/v1/claims/search',{"players.user_id":self.seller,"players.role":"respondent","status":status},keys=("data","results"),offset_limit=9950)
            for claim in batch:all_claims[str(claim['id'])]=claim
        self.claims=list(all_claims.values())
        from .api_completion import collect_claim_details
        await collect_claim_details(self)
    async def run_returns(self):
        for c in self.claims:
            if c.get('type') in ('return','cancel_sale','cancel_purchase','change'):
                self.save(str(c['id']),'/post-purchase/v1/claims/search',c)
    async def run_promotions(self):
        promotions=await self.pages(f'/seller-promotions/users/{self.seller}',{"app_version":"v2"})
        for p in promotions:
            if p.get('id') and p.get('type'):await self.pages(f"/seller-promotions/promotions/{quote(str(p['id']),safe='')}/items",{"promotion_type":p['type'],"app_version":"v2"})
    async def run_full_stock(self):
        ids=set()
        for i in self.items:
            for v in [i,*i.get('variations',[])]:
                if v.get('inventory_id'):ids.add(str(v['inventory_id']))
        for id in sorted(ids):await self.capture(f'/inventories/{quote(id,safe="")}/stock/fulfillment',id,{"include_attributes":"conditions"})
    async def run_categories(self):
        for id in sorted({i['catalog_product_id'] for i in self.items if i.get('catalog_product_id')}):
            await self.capture('/products/'+id,'catalog:'+id)
        for id in sorted({i['category_id'] for i in self.items if i.get('category_id')}):
            await self.capture('/categories/'+id,id)
            await self.capture('/categories/'+id+'/attributes',id+':attributes')
    async def billing_cached(self,path,key,params):
        previous=self.db.query(Snapshot).filter_by(seller_id=self.seller,module='billing',external_id=key).first()
        if previous:
            stamp=previous.fetched_at
            if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
            if now()-stamp<timedelta(hours=24):
                self.identifiers.add(key);return json.loads(decrypt(previous.payload_encrypted))
        return await self.capture(path,key,params)
    async def run_billing(self):
        for group in ('ML','MP'):
            for typ in ('BILL','CREDIT_NOTE'):
                offset=0
                while offset<2400:
                    periods=await self.billing_cached('/billing/integration/monthly/periods',f'{group}:{typ}:{offset}',{"group":group,"document_type":typ,"limit":12,"offset":offset})
                    if periods is None:break
                    batch=periods.get('results',[])
                    for p in batch:
                        key=p.get('key')
                        if not key:continue
                        document_offset=0
                        while document_offset<10000:
                            documents=await self.billing_cached(f'/billing/integration/periods/key/{quote(key,safe="")}/documents',f'documents:{group}:{typ}:{key}:{document_offset}',{"group":group,"document_type":typ,"offset":document_offset,"limit":100})
                            if documents is None:break
                            batch_docs=documents.get('results',documents.get('documents',[]))
                            if not isinstance(batch_docs,list):self.partial=True;break
                            document_offset+=len(batch_docs)
                            if not batch_docs or document_offset>=int(documents.get('total',document_offset)):break
                        else:self.partial=True
                        await self.billing_cached(f'/billing/integration/periods/key/{quote(key,safe="")}/summary/details',f'summary:{group}:{typ}:{key}',{"group":group,"document_type":typ})
                        cursor=0
                        for page in range(200):
                            path=f'/billing/integration/periods/key/{quote(key,safe="")}/group/{group}/details'
                            details=await self.billing_cached(path,f'details:{group}:{typ}:{key}:{cursor}',{"document_type":typ,"limit":1000,"from_id":cursor,"sort_by":"ID","order_by":"ASC"})
                            if details is None:break
                            results=details.get('results',details.get('details',[]))
                            if not isinstance(results,list):self.partial=True;break
                            next_id=details.get('last_id') or details.get('paging',{}).get('last_id')
                            if not results:break
                            if not next_id:
                                if len(results)>=1000 or int(details.get('total',len(results)))>len(results):self.partial=True
                                break
                            if str(next_id)==str(cursor):self.partial=True;break
                            cursor=next_id
                        else:self.partial=True
                    offset+=len(batch)
                    if not batch or offset>=int(periods.get('total',offset)):break
                else:self.partial=True
    async def run_ads(self):
        for typ,segment in [('PADS','product_ads'),('BADS','brand_ads'),('DISPLAY','display')]:
            data=await self.capture('/advertising/advertisers',typ,{"product_id":typ},{"Api-Version":"1"})
            for a in (data or {}).get('advertisers',[]):
                id=str(a['advertiser_id']);site=a.get('site_id','MLB')
                path=f'/advertising/{site}/advertisers/{id}/product_ads/campaigns/search' if typ=='PADS' else f'/advertising/advertisers/{id}/{segment}/campaigns'
                await self.pages(path,{},keys=("results","campaigns"),headers={"Api-Version":"2" if typ=='PADS' else "1"})

    async def run_ads_metrics(self):
        from .api_completion import collect_ads_metrics
        await collect_ads_metrics(self)
    async def run_financial(self):
        from .api_completion import collect_financial
        await collect_financial(self)

async def execute_job(job_id):
    with SessionLocal() as db:
        job=db.get(DataJob,job_id)
        try:
            c=latest(db)
            if not c or c.seller_id!=job.seller_id:raise ValueError('Account changed')
            headers=await MercadoLivreClient(db).authorization_headers(c.id)
            async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
                hub=Hub(db,job,headers,client)
                for module in MODULES:
                    hub.check();hub.module=module;hub.errors=[];hub.identifiers=set();hub.partial=False
                    job.progress={**job.progress,module:{"status":"running","count":0}};db.commit()
                    try:await getattr(hub,'run_'+module)()
                    except asyncio.CancelledError:raise
                    except Exception as e:
                        db.rollback();hub.errors.append({"http":getattr(e,'code',None),"issue":"provider_or_format"})
                    codes={e.get('http') for e in hub.errors};count=len(hub.identifiers)
                    status='partial' if hub.partial or (hub.errors and count) else 'denied' if codes & {401,403} else 'unavailable' if 404 in codes else 'failed' if hub.errors else 'success' if count else 'empty'
                    deps={'descriptions':'listings','prices':'listings','fees':'listings','shipping_quotes':'listings','full_stock':'listings','categories':'listings','shipments':'orders','shipment_costs':'orders','messages':'orders','returns':'claims','ads_metrics':'ads'}
                    dep=deps.get(module)
                    if not count and dep and job.progress.get(dep,{}).get('status') in ('denied','unavailable','failed','blocked'):
                        status='blocked';hub.errors.append({'issue':'dependency','source':dep})
                    job.progress={**job.progress,module:{"status":status,"count":count,"errors":hub.errors[:20],"updated_at":now().isoformat(),"action":('Verifique se Publicidade está habilitada na conta; a API não retornou anunciante.' if module in ('ads','ads_metrics') and status in ('unavailable','blocked','denied') else 'A API restringiu detalhes; consulte os papéis da conta na ocorrência.' if module=='claims' and 403 in codes else '')}};db.commit()
            job.status='completed_with_gaps' if any(v['status'] in ('partial','denied','unavailable','failed','blocked') for v in job.progress.values() if isinstance(v,dict)) else 'completed'
        except asyncio.CancelledError:db.rollback();job=db.get(DataJob,job_id);job.status='cancelled'
        except Exception:db.rollback();job=db.get(DataJob,job_id);job.status='failed'
        job.finished_at=now();db.add(AuditEvent(event_type='data_hub.sync_finished',entity_type='data_job',entity_id=str(job.id),detail={"status":job.status}));db.commit()

def job_out(j):return {"id":j.id,"status":j.status,"started_at":j.started_at,"finished_at":j.finished_at,"progress":j.progress}
@router.post('/sync',status_code=202)
def start(background:BackgroundTasks,db:Session=Depends(get_db)):
    c=latest(db)
    if not c:raise HTTPException(409,'Conecte sua conta do Mercado Livre em Ajustes.')
    job=DataJob(seller_id=c.seller_id,status='running',progress={});db.add(job)
    try:db.commit()
    except IntegrityError:db.rollback();raise HTTPException(409,'Já existe uma sincronização em andamento.')
    background.add_task(execute_job,job.id)
    return job_out(job)
@router.get('/status')
def status(db:Session=Depends(get_db)):
    c=latest(db)
    if not c:return {"connected":False,"modules":MODULES,"job":None,"counts":{}}
    j=db.query(DataJob).filter_by(seller_id=c.seller_id).order_by(DataJob.id.desc()).first()
    counts=dict(db.query(Snapshot.module,func.count(Snapshot.id)).filter_by(seller_id=c.seller_id).group_by(Snapshot.module).all())
    return {"connected":True,"modules":MODULES,"job":job_out(j) if j else None,"counts":counts}
@router.post('/cancel')
def cancel(db:Session=Depends(get_db)):
    c=latest(db)
    j=db.query(DataJob).filter_by(seller_id=c.seller_id,status='running').first() if c else None
    if not j:raise HTTPException(409,'Nenhuma sincronização ativa.')
    # Preserve the unique running state; cancellation is recorded in progress and checked by the worker.
    j.progress={**j.progress,"_cancel":True};db.commit();return {"requested":True}
@router.get('/records')
def records(module:str=Query(max_length=40),q:str=Query(default='',max_length=100),offset:int=Query(default=0,ge=0),db:Session=Depends(get_db)):
    if module not in MODULES:raise HTTPException(422,'Área desconhecida.')
    c=latest(db)
    if not c:return {"results":[],"total":0}
    query=db.query(Snapshot).filter_by(seller_id=c.seller_id,module=module)
    if q:query=query.filter(Snapshot.external_id.contains(q,autoescape=True))
    return {"results":[{"id":r.id,"external_id":r.external_id,"source":r.source,"fetched_at":r.fetched_at} for r in query.order_by(Snapshot.id).offset(offset).limit(30)],"total":query.count(),"offset":offset}
@router.get('/records/{record_id}')
def record(record_id:int,db:Session=Depends(get_db)):
    from .access import protected_access_configured
    if not protected_access_configured(db):raise HTTPException(409,"Configure seu acesso privado antes de abrir detalhes da API.")
    c=latest(db);r=db.get(Snapshot,record_id)
    if not c or not r or r.seller_id!=c.seller_id:raise HTTPException(404,'Registro não encontrado.')
    return {"external_id":r.external_id,"source":r.source,"fetched_at":r.fetched_at,"data":json.loads(decrypt(r.payload_encrypted))}

@router.get('/product-facts')
def facts(db:Session=Depends(get_db)):
    c=latest(db)
    if not c:return []
    snapshots=db.query(Snapshot).filter(Snapshot.seller_id==c.seller_id,Snapshot.module.in_(['listings','fees','shipping_quotes','visits'])).all()
    indexed={(r.module,r.external_id):r for r in snapshots}
    rows=[]
    from .catalog_sync import products_query
    for p in products_query(db).filter(Product.sku.like('meli:%')):
        id=p.sku[5:];item_row=indexed.get(('listings',id))
        if not item_row:continue
        item=json.loads(decrypt(item_row.payload_encrypted));fee_row=indexed.get(('fees',id));ship_row=indexed.get(('shipping_quotes',id));visit_row=indexed.get(('visits',id))
        quote_data=json.loads(decrypt(fee_row.payload_encrypted)) if fee_row else None
        options=quote_data if isinstance(quote_data,list) else [quote_data] if isinstance(quote_data,dict) else []
        fee=next((x for x in options if x.get('listing_type_id')==item.get('listing_type_id')),None)
        shipping=json.loads(decrypt(ship_row.payload_encrypted)) if ship_row else None
        visits=json.loads(decrypt(visit_row.payload_encrypted)) if visit_row else None
        rows.append({'product_id':p.id,'item_id':id,'name':p.name,'price':item.get('price'),'available_quantity':item.get('available_quantity'),'status':item.get('status'),'listing_type':item.get('listing_type_id'),'currency':item.get('currency_id'),'logistics':item.get('shipping',{}).get('logistic_type'),
            'variations':len(item.get('variations',[])),'fee_amount_estimate':fee.get('sale_fee_amount') if fee else None,
            'fee_percent_estimate':fee.get('sale_fee_details',{}).get('percentage_fee') if fee else None,'fee_fixed_estimate':fee.get('sale_fee_details',{}).get('fixed_fee') if fee else None,
            'shipping_quote_available':shipping is not None,'visits_90_days':visits.get('total_visits') if isinstance(visits,dict) else None,
            'fetched_at':item_row.fetched_at,'fee_fetched_at':fee_row.fetched_at if fee_row else None,'source':'Mercado Livre API'})
    return rows
