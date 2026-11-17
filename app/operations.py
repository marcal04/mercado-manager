"""Local operations only. This module never writes to the marketplace."""
import csv
import io
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, Numeric, String, Integer, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column, Session
from .config import get_settings
from .db import Base, get_db
from .models import Product, Approval, ApprovalStatus, AuditEvent, MarketplaceConnection, now
from .services.approval import ApprovalService

router = APIRouter(prefix="/api/v1")
class Economics(Base):
    __tablename__ = "product_economics"
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    cost: Mapped[Decimal] = mapped_column(Numeric(12,2))
    fee_percent: Mapped[Decimal] = mapped_column(Numeric(6,2))
    shipping: Mapped[Decimal] = mapped_column(Numeric(12,2))
    tax_percent: Mapped[Decimal] = mapped_column(Numeric(6,2))
    minimum_margin: Mapped[Decimal] = mapped_column(Numeric(6,2))
    minimum_stock: Mapped[int] = mapped_column(Integer, default=3)
class Inventory(Base):
    __tablename__ = "local_inventory"
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
class Movement(Base):
    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
class Order(Base):
    __tablename__ = "local_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(80), unique=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12,2))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
class Campaign(Base):
    __tablename__ = "campaign_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    budget: Mapped[Decimal] = mapped_column(Numeric(12,2))
    objective: Mapped[str] = mapped_column(Text)
    approval_id: Mapped[int] = mapped_column(ForeignKey("approvals.id"))

Money = Decimal
class CostInput(BaseModel):
    cost: Money = Field(ge=0, le=10000000, decimal_places=2)
    fee_percent: Money = Field(ge=0, le=99, decimal_places=2)
    shipping: Money = Field(ge=0, le=10000000, decimal_places=2)
    tax_percent: Money = Field(ge=0, le=99, decimal_places=2)
    minimum_margin: Money = Field(ge=0, le=99, decimal_places=2)
    minimum_stock: int = Field(ge=0, le=1000000)
class StockInput(BaseModel):
    delta: int = Field(ge=-1000000, le=1000000)
    reason: str = Field(min_length=3, max_length=500)
    actor: str = Field(min_length=2, max_length=120)
class OrderInput(BaseModel):
    reference: str = Field(min_length=1, max_length=80)
    product_id: int
    quantity: int = Field(gt=0, le=1000000)
    unit_price: Money = Field(gt=0, le=10000000, decimal_places=2)
class Transition(BaseModel):
    status: str = Field(pattern="^(confirmed|shipped|completed|cancelled)$")
    actor: str = Field(min_length=2, max_length=120)
class CampaignInput(BaseModel):
    name: str = Field(min_length=3, max_length=160)
    product_id: int
    budget: Money = Field(gt=0, le=10000000, decimal_places=2)
    objective: str = Field(min_length=5, max_length=2000)

def product(db, product_id):
    item=db.get(Product, product_id)
    if not item: raise HTTPException(404, "Produto não encontrado")
    return item

def audit(db, event, entity, identity, detail):
    db.add(AuditEvent(event_type=event, entity_type=entity, entity_id=str(identity), detail=detail))

def money(value): return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def catalog(db,archived=False):
    from .catalog_sync import products_query
    rows=[]
    for p in products_query(db,archived).order_by(Product.id).all():
        from .stock_profile import StockProfile, profile_out
        e=db.get(Economics,p.id); stock=db.get(Inventory,p.id)
        row={"id":p.id,"sku":p.sku,"name":p.name,"brand":p.brand,"price":p.reference_price,
             "marketplace_stock":p.stock,"local_stock":stock.quantity if stock else None,"economics":None}
        if e:
            price=Decimal(str(p.reference_price)); rates=(e.fee_percent+e.tax_percent)/100
            profit=price*(1-rates)-e.cost-e.shipping
            denominator=1-rates-e.minimum_margin/100
            row["economics"]={k:float(getattr(e,k)) for k in ("cost","fee_percent","shipping","tax_percent","minimum_margin")}
            row["economics"].update(minimum_stock=e.minimum_stock, profit=money(profit),
                margin=money(profit/price*100) if price else None,
                target_price=float(((e.cost+e.shipping)/denominator).quantize(Decimal("0.01"),rounding=ROUND_CEILING)) if denominator>0 else None)
        row["stock_profile"]=profile_out(db.get(StockProfile,p.id))
        rows.append(row)
    return rows

@router.get("/catalog")
def get_catalog(db:Session=Depends(get_db)): return catalog(db)

@router.get("/catalog/archived")
def archived_catalog(db:Session=Depends(get_db)):
    from .catalog_sync import ListingState
    rows=catalog(db,archived=True)
    for row in rows:
        lifecycle=db.get(ListingState,row["id"])
        row["marketplace_status"]=lifecycle.status;row["status_checked_at"]=lifecycle.checked_at
    return rows

@router.put("/products/{product_id}/economics")
def save_costs(product_id:int, body:CostInput, db:Session=Depends(get_db)):
    p=db.query(Product).filter_by(id=product_id).with_for_update().first()
    if not p:raise HTTPException(404,"Produto não encontrado")
    from .stock_profile import StockProfile
    profile=db.get(StockProfile,product_id)
    if profile and profile.mode=='owned' and body.cost!=0:
        raise HTTPException(422,"Estoque próprio sem compra tem custo de aquisição zero. Altere a origem se houve compra.")
    if body.fee_percent+body.tax_percent+body.minimum_margin>=100:
        raise HTTPException(422,"Tarifas, impostos e margem devem somar menos de 100%")
    e=db.get(Economics,product_id)
    if not e: e=Economics(product_id=product_id); db.add(e)
    for key,value in body.model_dump().items(): setattr(e,key,value)
    if profile:profile.acquisition_cost=body.cost;profile.updated_at=now()
    audit(db,"economics.updated","product",product_id,body.model_dump(mode="json")); db.commit()
    return {"saved":True,"scope":"local"}

def change_stock(db, product_id, delta, reason, actor):
    # Lock the product even on the first local stock entry, serializing concurrent adjustments.
    p=db.query(Product).filter(Product.id==product_id).with_for_update().first()
    if not p: raise HTTPException(404,"Produto não encontrado")
    item=db.get(Inventory,product_id)
    if not item: item=Inventory(product_id=product_id,quantity=0); db.add(item)
    if item.quantity+delta<0: raise HTTPException(409,"Estoque local insuficiente")
    item.quantity+=delta
    db.add(Movement(product_id=product_id,delta=delta,reason=reason,actor=actor))
    audit(db,"stock.adjusted","product",product_id,{"delta":delta,"reason":reason,"actor":actor})
    return item.quantity

@router.post("/products/{product_id}/stock")
def stock_change(product_id:int,body:StockInput,db:Session=Depends(get_db)):
    quantity=change_stock(db,product_id,body.delta,body.reason,body.actor); db.commit()
    return {"local_stock":quantity,"scope":"local"}

@router.get("/stock-movements")
def movements(db:Session=Depends(get_db)):
    return [{"id":m.id,"product_id":m.product_id,"delta":m.delta,"reason":m.reason,"actor":m.actor,"created_at":m.created_at} for m in db.query(Movement).order_by(Movement.id.desc()).limit(200)]

def order_out(o):
    return {"id":o.id,"reference":o.reference,"product_id":o.product_id,"quantity":o.quantity,
            "unit_price":float(o.unit_price),"total":money(o.unit_price*o.quantity),"status":o.status,"created_at":o.created_at}
@router.get("/orders")
def orders(db:Session=Depends(get_db)): return [order_out(o) for o in db.query(Order).order_by(Order.id.desc())]
@router.post("/orders",status_code=201)
def new_order(body:OrderInput,db:Session=Depends(get_db)):
    product(db,body.product_id)
    if db.query(Order).filter_by(reference=body.reference).first(): raise HTTPException(409,"Referência já cadastrada")
    o=Order(**body.model_dump()); db.add(o); db.flush()
    audit(db,"order.created","order",o.id,{"reference":o.reference}); db.commit(); return order_out(o)
@router.post("/orders/{order_id}/status")
def transition(order_id:int,body:Transition,db:Session=Depends(get_db)):
    o=db.query(Order).filter(Order.id==order_id).with_for_update().first()
    if not o: raise HTTPException(404,"Pedido não encontrado")
    allowed={"pending":{"confirmed","cancelled"},"confirmed":{"shipped","cancelled"},"shipped":{"completed"},"completed":set(),"cancelled":set()}
    if body.status not in allowed[o.status]: raise HTTPException(409,"Transição de pedido inválida")
    previous=o.status
    if body.status=="confirmed": change_stock(db,o.product_id,-o.quantity,f"Pedido {o.reference}",body.actor)
    if body.status=="cancelled" and previous=="confirmed": change_stock(db,o.product_id,o.quantity,f"Cancelamento {o.reference}",body.actor)
    o.status=body.status
    audit(db,"order.status_changed","order",o.id,{"from":previous,"to":o.status,"actor":body.actor}); db.commit()
    return order_out(o)
@router.post("/campaigns",status_code=201)
def campaign(body:CampaignInput,db:Session=Depends(get_db)):
    p=product(db,body.product_id)
    proposal={"title":body.name,"summary":f"Rascunho para {p.name}. Objetivo: {body.objective}. Orçamento total proposto: R$ {body.budget:.2f}.",
        "benefits":["Permite revisar objetivo e limite antes de gastar."],
        "risks":["Retorno não garantido; faltam estimativas de conversão e custo por clique."],
        "assumptions":["Rascunho local. Aprovar apenas registra a decisão; não cria campanha nem gasta."],
        "next_step":"Revisar margem e materiais de divulgação antes de uma execução futura."}
    a=Approval(action_type="campaign_draft",payload={"proposal":proposal,**body.model_dump(mode="json")},risk_note=proposal["risks"][0]); db.add(a); db.flush()
    c=Campaign(**body.model_dump(),approval_id=a.id); db.add(c); db.flush()
    audit(db,"campaign.drafted","campaign",c.id,{"approval_id":a.id,"simulated":True}); db.commit()
    return {"id":c.id,"approval_id":a.id,"simulated":True}
@router.get("/campaigns")
def campaigns(db:Session=Depends(get_db)):
    return [{"id":c.id,"name":c.name,"product_id":c.product_id,"budget":float(c.budget),"objective":c.objective,
       "approval_id":c.approval_id,"status":db.get(Approval,c.approval_id).status.value,"simulated":True} for c in db.query(Campaign).order_by(Campaign.id.desc())]
@router.get("/overview")
def overview(db:Session=Depends(get_db)):
    from .seller_proceeds import cached_results
    quotes={r["product_id"]:r for r in cached_results(db)}
    items=catalog(db); alerts=[]
    for p in items:
        e=p["economics"]
        quote=quotes.get(p["id"])
        if quote and quote["state"]=="estimated":
            if quote["meets_target"] is False:alerts.append({"product_id":p["id"],"message":f'{p["name"]}: sobra estimada abaixo da meta.'})
            elif quote["minimum_margin"] is None:alerts.append({"product_id":p["id"],"message":f'{p["name"]}: defina sua meta de margem.'})
        elif not e: alerts.append({"product_id":p["id"],"message":p["name"]+": atualize a cotação automática ou confirme o custo de aquisição."})
        elif e["margin"] is not None and e["margin"]<e["minimum_margin"]: alerts.append({"product_id":p["id"],"message":f'{p["name"]}: margem abaixo da meta.'})
        if p["local_stock"] is None: alerts.append({"product_id":p["id"],"message":f'{p["name"]}: estoque físico ainda não conferido.'})
        elif p["local_stock"]<=(e["minimum_stock"] if e else 3): alerts.append({"product_id":p["id"],"message":f'{p["name"]}: estoque local baixo.'})
    accepted=db.query(Order).filter(Order.status.in_(["confirmed","shipped","completed"])).all()
    return {"products":len(items),"pending_approvals":db.query(Approval).filter_by(status=ApprovalStatus.pending).count(),
        "local_order_revenue":money(sum((o.unit_price*o.quantity for o in accepted),Decimal(0))),
        "open_orders":db.query(Order).filter(Order.status.in_(["pending","confirmed","shipped"])).count(),
        "marketplace_connected":db.query(MarketplaceConnection).count()>0,"alerts":alerts,"ai_enabled":get_settings().ai_enabled,"scope":"local"}
@router.get("/audit")
def events(db:Session=Depends(get_db)):
    return [{"id":e.id,"event_type":e.event_type,"entity_type":e.entity_type,"entity_id":e.entity_id,"detail":e.detail,"created_at":e.created_at} for e in db.query(AuditEvent).order_by(AuditEvent.id.desc()).limit(200)]
@router.get("/reports/catalog.csv")
def export_catalog(db:Session=Depends(get_db)):
    output=io.StringIO(); w=csv.writer(output,delimiter=";"); w.writerow(["SKU","Produto","Preço publicado","Estoque ML","Estoque local","Lucro unitário estimado","Margem %"])
    def cell(s):
        value=str(s)
        return "'"+value if value.startswith(("=","+","-","@","\t","\r","\n")) else value
    for p in catalog(db):
        e=p["economics"] or {}; w.writerow([cell(p["sku"]),cell(p["name"]),p["price"],p["marketplace_stock"],p["local_stock"],e.get("profit"),e.get("margin")])
    return Response(content="\ufeff"+output.getvalue(),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=catalogo.csv"})

class PriceInput(BaseModel):
    proposed_price: Money = Field(gt=0, le=10000000, decimal_places=2)
    reason: str = Field(min_length=5, max_length=2000)

@router.post("/products/{product_id}/price-proposals", status_code=201)
def price_proposal(product_id:int,body:PriceInput,db:Session=Depends(get_db)):
    p=product(db,product_id); e=db.get(Economics,product_id)
    if not e: raise HTTPException(409,"Cadastre os custos antes de propor um preço.")
    profit=body.proposed_price*(1-(e.fee_percent+e.tax_percent)/100)-e.cost-e.shipping
    margin=money(profit/body.proposed_price*100)
    proposal={"title":f"Revisão de preço: {p.name}",
        "summary":f"Preço publicado R$ {p.reference_price:.2f}; proposto R$ {body.proposed_price:.2f}. Motivo: {body.reason}",
        "benefits":[f"Resultado unitário estimado: R$ {money(profit):.2f}; margem {margin:.2f}%."],
        "risks":["Preço não considera concorrência, publicidade ou devoluções em tempo real."] + (["A margem proposta está abaixo da meta cadastrada."] if margin<float(e.minimum_margin) else []),
        "assumptions":[f"Custo R$ {e.cost:.2f}, tarifa {e.fee_percent}%, imposto {e.tax_percent}%, frete R$ {e.shipping:.2f}.","Aprovar não altera o preço publicado."],
        "next_step":"Conferir as premissas e registrar sua decisão. Execução externa fica para uma etapa futura."}
    a=ApprovalService(db).request("price_proposal",{"product_id":product_id,"current_price":p.reference_price,"proposed_price":str(body.proposed_price),"proposal":proposal},"; ".join(proposal["risks"]))
    return {"id":a.id,"simulated":True,"proposal":proposal}
