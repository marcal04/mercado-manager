"""Deterministic BRL price scenarios. No provider calls or marketplace writes."""
import hashlib
import json
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP, ROUND_FLOOR
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, JSON, DateTime
from sqlalchemy.orm import Mapped, mapped_column, Session
from .db import Base, get_db
from .models import Product, now
from .operations import Economics, product, audit
from .services.approval import ApprovalService

router=APIRouter(prefix="/api/v1/pricing")
CENT=Decimal("0.01")
class PriceSimulation(Base):
    __tablename__="price_simulations"
    id:Mapped[int]=mapped_column(primary_key=True)
    product_id:Mapped[int]=mapped_column(ForeignKey("products.id"),index=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    snapshot:Mapped[dict]=mapped_column(JSON)
class SimulationInput(BaseModel):
    product_id:int
    proposed_price:Decimal|None=Field(default=None,gt=0,le=10000000,decimal_places=2)
    competitor_price:Decimal|None=Field(default=None,gt=0,le=10000000,decimal_places=2)
    undercut_percent:Decimal=Field(default=Decimal("1"),ge=0,le=20,decimal_places=2)
class ProposalInput(BaseModel):
    scenario:str=Field(min_length=1,max_length=40)
    actor:str=Field(min_length=2,max_length=120)

def quantize(n,rounding=ROUND_HALF_UP):return n.quantize(CENT,rounding=rounding)
def as_money(n):return str(quantize(n))
def assumptions(p,e):
    return {"product_id":p.id,"name":p.name,"sku":p.sku,"current_price":as_money(Decimal(str(p.reference_price))),
        **{k:as_money(getattr(e,k)) for k in ("cost","fee_percent","shipping","tax_percent","minimum_margin")}}
def fingerprint(values):return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()
def calculate(p,e,body):
    a=assumptions(p,e);current=Decimal(a["current_price"])
    rates=(e.fee_percent+e.tax_percent)/100
    if current<=0 or rates+e.minimum_margin/100>=1:raise HTTPException(409,"Confira o preço atual, as tarifas e a margem cadastrada.")
    fixed=e.cost+e.shipping
    floor=max(CENT,quantize(fixed/(1-rates-e.minimum_margin/100),ROUND_CEILING))
    break_even=max(CENT,quantize(fixed/(1-rates),ROUND_CEILING))
    def scenario(key,label,price):
        price=max(CENT,quantize(price));fee=price*e.fee_percent/100;tax=price*e.tax_percent/100
        profit=price-fee-tax-fixed;margin=profit/price*100
        return {"key":key,"label":label,"price":as_money(price),"profit":as_money(profit),"margin":as_money(margin),
            "fee":as_money(fee),"tax":as_money(tax),"cost":as_money(e.cost),"shipping":as_money(e.shipping),
            "price_change_percent":as_money((price/current-1)*100),"meets_target":margin>=e.minimum_margin,
            "status":"loss" if profit<0 else "below_target" if margin<e.minimum_margin else "within_target"}
    rows=[scenario("current","Preço atual",current),scenario("discount5","Desconto de 5%",current*Decimal("0.95")),
        scenario("discount10","Desconto de 10%",current*Decimal("0.90")),scenario("minimum","Piso para sua margem",floor)]
    if body.proposed_price is not None:rows.append(scenario("custom","Preço que você informou",body.proposed_price))
    if body.competitor_price is not None:
        competitive=quantize(body.competitor_price*(1-body.undercut_percent/100))
        rows.append(scenario("competitive","Referência ajustada, respeitando sua margem",max(floor,competitive)))
    discount=max(Decimal(0),(current-floor)/current*100)
    return {"assumptions":a,"fingerprint":fingerprint(a),"inputs":body.model_dump(mode="json"),
        "minimum_price":as_money(floor),"break_even_price":as_money(break_even),"max_discount_percent":str(quantize(discount,ROUND_FLOOR)),
        "scenarios":rows,"notice":"Estimativa local em BRL. Não inclui publicidade, devoluções, juros, taxas fixas adicionais ou consulta automática de concorrentes. A referência concorrente é informada por você, sem validação de equivalência do produto."}

@router.post("/simulate",status_code=201)
def simulate(body:SimulationInput,db:Session=Depends(get_db)):
    p=product(db,body.product_id);e=db.get(Economics,p.id)
    if not e:raise HTTPException(409,"Cadastre os custos do produto em Catálogo → Custos e margem antes de simular.")
    result=calculate(p,e,body);row=PriceSimulation(product_id=p.id,snapshot=result);db.add(row);db.flush()
    audit(db,"pricing.simulated","simulation",row.id,{"product_id":p.id,"mode":"local"});db.commit()
    return {"id":row.id,"created_at":row.created_at,**result}
@router.get("/simulations")
def history(product_id:int|None=None,db:Session=Depends(get_db)):
    query=db.query(PriceSimulation)
    if product_id is not None:query=query.filter_by(product_id=product_id)
    return [{"id":s.id,"product_id":s.product_id,"created_at":s.created_at,**s.snapshot} for s in query.order_by(PriceSimulation.id.desc()).limit(50)]
@router.post("/simulations/{simulation_id}/proposal",status_code=201)
def propose(simulation_id:int,body:ProposalInput,db:Session=Depends(get_db)):
    row=db.get(PriceSimulation,simulation_id)
    if not row:raise HTTPException(404,"Simulação não encontrada.")
    p=product(db,row.product_id);e=db.get(Economics,p.id)
    if not e or fingerprint(assumptions(p,e))!=row.snapshot["fingerprint"]:
        raise HTTPException(409,"Os custos ou o preço do produto mudaram. Faça uma nova simulação antes de criar a proposta.")
    scenario=next((s for s in row.snapshot["scenarios"] if s["key"]==body.scenario),None)
    if not scenario:raise HTTPException(422,"Cenário não encontrado nesta simulação.")
    if not scenario["meets_target"]:raise HTTPException(409,"Este cenário fica abaixo da margem mínima. Ajuste o preço e simule novamente.")
    if Decimal(scenario["price"])>10000000:raise HTTPException(409,"Preço calculado excede o limite operacional. Confira os custos.")
    if scenario["price"]==row.snapshot["assumptions"]["current_price"]:
        raise HTTPException(409,"O cenário mantém o preço atual; não há alteração a propor.")
    from .models import Approval,ApprovalStatus
    existing=db.query(Approval).filter_by(action_type="simulated_price",status=ApprovalStatus.pending).all()
    if any(a.payload.get("simulation_id")==row.id and a.payload.get("scenario_key")==body.scenario for a in existing):
        raise HTTPException(409,"Já existe uma proposta pendente para este cenário.")
    proposal={"title":f"Revisar preço de {p.name}","summary":f"De R$ {p.reference_price:.2f} para R$ {scenario['price']}. Cenário: {scenario['label']}.",
        "benefits":[f"Resultado unitário estimado de R$ {scenario['profit']} e margem de {scenario['margin']}%.","Mantém a margem mínima com as premissas cadastradas."],
        "risks":["Não há garantia de vendas ou de competitividade.","Frete, tarifas e custos precisam ser conferidos; publicidade e devoluções não foram incluídas."],
        "assumptions":[f"Custo R$ {e.cost}, frete R$ {e.shipping}, tarifa {e.fee_percent}%, imposto {e.tax_percent}%, meta {e.minimum_margin}%.",row.snapshot["notice"]],
        "next_step":"Revise as premissas. Aprovar registra somente uma decisão simulada, sem alterar o anúncio."}
    approval=ApprovalService(db).request("simulated_price",{"simulation_id":row.id,"scenario_key":body.scenario,"product_id":p.id,"actor":body.actor,"proposed_price":scenario["price"],"proposal":proposal,"calculation":row.snapshot},"; ".join(proposal["risks"]))
    return {"approval_id":approval.id,"simulated":True}
