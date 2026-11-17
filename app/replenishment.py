"""Local replenishment planning; never places purchases or changes stock."""
import hashlib
import json
from datetime import datetime,timedelta,timezone
from decimal import Decimal,ROUND_CEILING
from fastapi import APIRouter,Depends,HTTPException,Query
from pydantic import BaseModel,Field
from sqlalchemy import ForeignKey,String,Integer,Numeric,DateTime,func
from sqlalchemy.orm import Mapped,mapped_column,Session
from .db import Base,get_db
from .models import Product,Approval,ApprovalStatus,now
from .operations import Economics,Inventory,Order,product,audit
from .marketplace_orders import ImportedOrder,SyncRun,latest
from .services.approval import ApprovalService

router=APIRouter(prefix="/api/v1/replenishment")
class Policy(Base):
    __tablename__="replenishment_policies"
    product_id:Mapped[int]=mapped_column(ForeignKey("products.id"),primary_key=True)
    supplier:Mapped[str]=mapped_column(String(160))
    lead_days:Mapped[int]=mapped_column(Integer)
    coverage_days:Mapped[int]=mapped_column(Integer)
    safety_units:Mapped[int]=mapped_column(Integer)
    pack_size:Mapped[int]=mapped_column(Integer)
    minimum_order:Mapped[int]=mapped_column(Integer)
    incoming_units:Mapped[int]=mapped_column(Integer)
    daily_demand:Mapped[Decimal|None]=mapped_column(Numeric(12,4),nullable=True)
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class PolicyInput(BaseModel):
    supplier:str=Field(min_length=2,max_length=160)
    lead_days:int=Field(ge=1,le=180)
    coverage_days:int=Field(ge=1,le=365)
    safety_units:int=Field(ge=0,le=1000000)
    pack_size:int=Field(ge=1,le=1000000)
    minimum_order:int=Field(ge=1,le=1000000)
    incoming_units:int=Field(ge=0,le=1000000)
    daily_demand:Decimal|None=Field(default=None,ge=0,le=100000,decimal_places=4)
    actor:str=Field(min_length=2,max_length=120)
class ProposalInput(BaseModel):
    fingerprint:str=Field(min_length=64,max_length=64)
    actor:str=Field(min_length=2,max_length=120)

def aware(d):return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
def ceil(n):return int(Decimal(n).to_integral_value(rounding=ROUND_CEILING))
def policy_out(policy):
    if not policy:return None
    return {k:(str(getattr(policy,k)) if k=="daily_demand" and getattr(policy,k) is not None else getattr(policy,k)) for k in ("supplier","lead_days","coverage_days","safety_units","pack_size","minimum_order","incoming_units","daily_demand")}

def demand_history(db,days):
    c=latest(db)
    if not c:return {},"missing_history",None
    run=db.query(SyncRun).filter_by(seller_id=c.seller_id).order_by(SyncRun.id.desc()).first()
    if not run or not run.finished_at:return {},"missing_history",None
    end=aware(run.finished_at)
    if run.status!="success" or run.days<days:return {},"incomplete_history",end
    if now()-end>timedelta(hours=24):return {},"stale_history",end
    totals={}
    for o in db.query(ImportedOrder).filter(ImportedOrder.seller_id==c.seller_id,ImportedOrder.status=="paid",ImportedOrder.created_at>=end-timedelta(days=days),ImportedOrder.created_at<=end):
        for item in o.items:
            # One catalog row aggregates all variations of the same marketplace listing.
            sku="meli:"+item["item_id"];totals[sku]=totals.get(sku,0)+max(0,int(item["quantity"]))
    return totals,"ready",end

def build_plan(db,days,stock_source="physical"):
    sales,quality,cutoff=demand_history(db,days);rows=[]
    reserved=dict(db.query(Order.product_id,func.sum(Order.quantity)).filter_by(status="pending").group_by(Order.product_id).all())
    from .catalog_sync import products_query
    for p in products_query(db).order_by(Product.id):
        from .stock_profile import StockProfile
        profile=db.get(StockProfile,p.id);owned=bool(profile and profile.mode=='owned')
        policy=db.get(Policy,p.id);stock=db.get(Inventory,p.id);costs=db.get(Economics,p.id)
        data=policy_out(policy);warnings=[];blockers=[]
        base_stock=stock.quantity if stock else None
        stock_timestamp=None
        if stock_source=="marketplace":
            from .data_hub import Snapshot
            c=latest(db)
            source=db.query(Snapshot).filter_by(seller_id=c.seller_id,module="listings",external_id=p.sku[5:]).first() if c and p.sku.startswith("meli:") else None
            if source:
                from .security import decrypt
                payload=json.loads(decrypt(source.payload_encrypted))
                base_stock=payload.get("available_quantity");stock_timestamp=source.fetched_at.isoformat()
                warnings.append("Estoque-base é o anunciado na última sincronização da API; não representa contagem física.")
            else:base_stock=None

        daily=None;basis="unavailable";sold=None
        if policy and policy.daily_demand is not None:
            daily=policy.daily_demand;basis="manual";warnings.append("Demanda diária informada por você; não validada com vendas reais.")
        elif quality=="ready" and p.sku.startswith("meli:"):
            sold=sales.get(p.sku,0);daily=Decimal(sold)/days;basis="imported_paid"
            warnings.append("Média de pedidos pagos da última importação; não é garantia de demanda futura.")
        else:warnings.append("Histórico ausente, incompleto, antigo ou sem vínculo com anúncio. Importe pedidos para o período ou informe a demanda diária.")
        if not policy:blockers.append("Configure fornecedor e regras de reposição.")
        if base_stock is None:blockers.append("Sincronize os anúncios na Central de dados." if stock_source=="marketplace" else "Confira e registre o estoque físico local.")
        if costs is None:blockers.append("Confirme os custos do produto, inclusive quando a aquisição não teve custo.")
        if daily is None:blockers.append("Falta uma base de demanda utilizável.")
        target=quantity=coverage=None;available=None
        if policy and base_stock is not None:
            available=int(base_stock)+policy.incoming_units-int(reserved.get(p.id,0))
            if daily is not None:
                target=max(costs.minimum_stock if costs else 0,ceil(daily*(policy.lead_days+policy.coverage_days))+policy.safety_units)
                needed=max(0,target-available)
                quantity=ceil(Decimal(max(needed,policy.minimum_order))/policy.pack_size)*policy.pack_size if needed else 0
                coverage=round(float(Decimal(max(0,available))/daily),1) if daily>0 else None
                if quantity>1000000:blockers.append("Quantidade calculada excede o limite operacional; revise as regras.")
        amount=str((costs.cost*quantity).quantize(Decimal("0.01"))) if costs and quantity is not None else None
        if owned:
            blockers=[];target=quantity=coverage=amount=None
            warnings=["Estoque próprio sem custo de aquisição, informado por você. Fornecedor e reposição de compra não se aplicam."]
        row={"product_id":p.id,"name":p.name,"sku":p.sku,"policy":data,"physical_stock":stock.quantity if stock else None,"stock_source":stock_source,"stock_base":base_stock,"stock_fetched_at":stock_timestamp,
             "manual_pending_units":int(reserved.get(p.id,0)),"available_with_incoming":available,
             "sold_units":sold,"demand_basis":basis,"daily_demand":str(daily.quantize(Decimal("0.0001"))) if daily is not None else None,
             "history_quality":quality,"history_cutoff":cutoff.isoformat() if cutoff else None,"days":days,
             "target_stock":target,"suggested_quantity":quantity,"coverage_days":coverage,"estimated_cost":amount,
             "unit_cost":str(profile.acquisition_cost) if owned else str(costs.cost) if costs else None,"minimum_stock":costs.minimum_stock if costs else None,
             "stock_mode":profile.mode if profile else 'unconfirmed',
             "warnings":warnings,"blockers":blockers,"can_propose":not owned and not blockers and bool(quantity)}
        row["fingerprint"]=hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()
        rows.append(row)
    return {"results":rows,"history_quality":quality,"days":days,"history_cutoff":cutoff,"mode":"local_planning"}

@router.put("/products/{product_id}/policy")
def save_policy(product_id:int,body:PolicyInput,db:Session=Depends(get_db)):
    from .stock_profile import StockProfile
    origin=db.get(StockProfile,product_id)
    if origin and origin.mode=='owned':raise HTTPException(409,"Estoque próprio sem compra não usa reposição por fornecedor. Altere a origem no Catálogo se passar a comprar reposição.")
    product(db,product_id);policy=db.get(Policy,product_id)
    if not policy:policy=Policy(product_id=product_id);db.add(policy)
    for k,v in body.model_dump(exclude={"actor"}).items():setattr(policy,k,v)
    policy.updated_at=now();audit(db,"replenishment.policy_updated","product",product_id,body.model_dump(mode="json"));db.commit()
    return {"saved":True,"scope":"local"}
@router.get("/plan")
def plan(days:int=Query(default=30,ge=1,le=365),stock_source:str=Query(default="physical",pattern="^(physical|marketplace)$"),db:Session=Depends(get_db)):return build_plan(db,days,stock_source)
@router.post("/products/{product_id}/proposal",status_code=201)
def propose(product_id:int,body:ProposalInput,days:int=Query(default=30,ge=1,le=365),stock_source:str=Query(default="physical",pattern="^(physical|marketplace)$"),db:Session=Depends(get_db)):
    product(db,product_id)
    row=next(r for r in build_plan(db,days,stock_source)["results"] if r["product_id"]==product_id)
    if body.fingerprint!=row["fingerprint"]:raise HTTPException(409,"Estoque, custos ou premissas mudaram. Atualize o plano e revise novamente.")
    if not row["can_propose"]:raise HTTPException(409,"Não há reposição válida para propor. Confira as pendências e a quantidade.")
    for a in db.query(Approval).filter_by(action_type="replenishment_plan",status=ApprovalStatus.pending):
        if a.payload.get("product_id")==product_id:raise HTTPException(409,"Este produto já tem uma proposta de reposição pendente.")
    q=row["suggested_quantity"];policy=row["policy"]
    proposal={"title":f"Avaliar reposição: {row['name']}","summary":f"Comprar {q} unidade(s) de {policy['supplier']}. Custo de mercadoria estimado R$ {row['estimated_cost']}.",
        "benefits":[f"Planeja estoque-alvo de {row['target_stock']} unidades para prazo e cobertura cadastrados.","Considera pedidos manuais pendentes e mercadoria em trânsito informada."],
        "risks":["Estimativa não inclui frete de compra, descontos, impostos adicionais ou confirmação do fornecedor.","Vendas passadas e demanda manual não garantem vendas futuras. Pode haver excesso ou falta de estoque."],
        "assumptions":[f"Estoque-base ({row['stock_source']}) {row['stock_base']}, pendente manual {row['manual_pending_units']}, em trânsito {policy['incoming_units']}.",f"Demanda {row['daily_demand']}/dia, prazo {policy['lead_days']} dias, cobertura adicional {policy['coverage_days']} dias, segurança {policy['safety_units']} unidades.",*row["warnings"]],
        "next_step":"Confira orçamento, estoque físico e disponibilidade com o fornecedor. Aprovar só registra a decisão; não realiza compra nem entrada de estoque."}
    a=ApprovalService(db).request("replenishment_plan",{"product_id":product_id,"actor":body.actor,"plan":row,"proposal":proposal},"; ".join(proposal["risks"]))
    return {"approval_id":a.id,"simulated":True}
