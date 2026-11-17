"""Read-only order snapshots, deliberately independent of local stock and manual orders."""
import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import String, Numeric, DateTime, Integer, JSON, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, Session
from .db import Base, get_db
from .models import MarketplaceConnection, AuditEvent, now
from .services.mercadolivre import MercadoLivreClient, API_URL

router=APIRouter(prefix="/api/v1")
class ImportedOrder(Base):
    __tablename__="marketplace_orders"
    __table_args__=(UniqueConstraint("seller_id","external_id"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    seller_id:Mapped[str]=mapped_column(String(64),index=True)
    external_id:Mapped[str]=mapped_column(String(64))
    status:Mapped[str]=mapped_column(String(64))
    currency:Mapped[str]=mapped_column(String(8))
    total:Mapped[Decimal]=mapped_column(Numeric(16,2))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    imported_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    items:Mapped[list]=mapped_column(JSON)
class SyncRun(Base):
    __tablename__="marketplace_sync_runs"
    id:Mapped[int]=mapped_column(primary_key=True)
    seller_id:Mapped[str]=mapped_column(String(64),index=True)
    started_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    finished_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=True)
    status:Mapped[str]=mapped_column(String(32))
    count:Mapped[int]=mapped_column(Integer,default=0)
    days:Mapped[int]=mapped_column(Integer)
    note:Mapped[str]=mapped_column(String(500),default="")

def latest(db): return db.query(MarketplaceConnection).order_by(MarketplaceConnection.id.desc()).first()
def parsed_date(value):
    d=datetime.fromisoformat(value.replace("Z","+00:00"))
    if d.tzinfo is None: raise ValueError("Data sem fuso horário na resposta.")
    return d.astimezone(timezone.utc)
def amount(value):
    d=Decimal(str(value))
    if not d.is_finite() or abs(d)>Decimal("99999999999999.99"): raise ValueError("Valor monetário inválido.")
    return d.quantize(Decimal("0.01"))
def normalize(order,seller_id):
    if str(order.get("seller",{}).get("id"))!=seller_id: raise ValueError("Pedido não pertence ao vendedor conectado.")
    # Allowlist only; buyer identity, addresses, payments and raw payloads are not stored.
    return dict(seller_id=seller_id,external_id=str(order["id"]),status=str(order["status"])[:64],
        currency=str(order["currency_id"])[:8],total=amount(order["total_amount"]),created_at=parsed_date(order["date_created"]),
        updated_at=parsed_date(order.get("date_last_updated") or order.get("last_updated") or order["date_created"]),imported_at=now(),
        items=[{"item_id":str(i["item"]["id"]),"title":str(i["item"].get("title",""))[:500],
            "variation_id":str(i["item"]["variation_id"]) if i["item"].get("variation_id") is not None else None,
            "quantity":int(i["quantity"]),"unit_price":str(amount(i["unit_price"]))} for i in order.get("order_items",[])])

async def import_orders(db,days):
    connection=latest(db)
    if not connection: raise HTTPException(409,"Conecte sua conta do Mercado Livre em Ajustes antes de importar.")
    seller_id=connection.seller_id; connection_id=connection.id
    run=SyncRun(seller_id=seller_id,status="running",days=days,note="Importação somente leitura")
    db.add(run);db.commit();run_id=run.id
    try:
        headers=await MercadoLivreClient(db).authorization_headers(connection_id)
        end=now();start=end-timedelta(days=days);offset=0;data={};total=0
        async with httpx.AsyncClient(timeout=25) as client:
            while offset<1000:
                response=await client.get(f"{API_URL}/orders/search",headers=headers,params={
                    "seller":seller_id,"sort":"date_desc","limit":50,"offset":offset,
                    "order.date_created.from":start.isoformat(timespec="milliseconds"),
                    "order.date_created.to":end.isoformat(timespec="milliseconds")})
                response.raise_for_status();payload=response.json()
                batch=payload["results"];total=int(payload["paging"]["total"])
                if not batch and offset<total: raise ValueError("Paginação incompleta retornada pelo Mercado Livre.")
                for order in batch:
                    row=normalize(order,seller_id);data[row["external_id"]]=row
                offset+=len(batch)
                if not batch or offset>=total:break
        truncated=offset<total
        # Upsert uses a database uniqueness constraint, keeping repeat/concurrent imports idempotent.
        if db.bind.dialect.name=="postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        for row in data.values():
            stmt=insert(ImportedOrder).values(**row)
            stmt=stmt.on_conflict_do_update(index_elements=["seller_id","external_id"],
                set_={k:stmt.excluded[k] for k in row if k not in ("seller_id","external_id")},
                where=stmt.excluded.updated_at>=ImportedOrder.updated_at)
            db.execute(stmt)
        run=db.get(SyncRun,run_id);run.status="partial" if truncated else "success";run.count=len(data);run.finished_at=now()
        run.note="Limite de 1.000 resultados; reduza o período para importar os demais." if truncated else "Importação concluída; estoque local preservado."
        db.add(AuditEvent(event_type="orders.imported",entity_type="sync",entity_id=str(run_id),detail={"count":len(data),"days":days,"partial":truncated,"mode":"read_only"}))
        db.commit()
        return {"imported":len(data),"total_found":total,"partial":truncated,"days":days,"mode":"read_only","message":run.note}
    except Exception as error:
        db.rollback()
        code=error.response.status_code if isinstance(error,httpx.HTTPStatusError) else None
        if code in (400,401): message="Autorização inválida ou expirada. Reconecte o Mercado Livre em Ajustes e tente novamente."
        elif code==403: message="Mercado Livre negou a leitura de pedidos. Confira as permissões de vendas da aplicação e reconecte a conta."
        elif code==429:message="Mercado Livre limitou as consultas. Aguarde um pouco antes de tentar novamente."
        else:message="Não foi possível importar os pedidos. Os dados da importação anterior foram preservados."
        run=db.get(SyncRun,run_id);run.status="failed";run.finished_at=now();run.note=message;db.commit()
        raise HTTPException(502,message) from None

@router.post("/integrations/mercadolivre/sync-orders")
async def sync_orders(days:int=Query(default=30,ge=1,le=365),db:Session=Depends(get_db)):
    return await import_orders(db,days)

def order_out(o):
    return {"id":o.external_id,"status":o.status,"currency":o.currency,"total":str(o.total),
        "created_at":o.created_at,"updated_at":o.updated_at,"imported_at":o.imported_at,"items":o.items,"source":"mercadolivre"}
@router.get("/marketplace-orders")
def get_orders(status:str|None=None,q:str=Query(default="",max_length=100),offset:int=Query(default=0,ge=0),limit:int=Query(default=50,ge=1,le=100),db:Session=Depends(get_db)):
    c=latest(db)
    if not c:return {"results":[],"total":0,"offset":offset,"limit":limit}
    query=db.query(ImportedOrder).filter_by(seller_id=c.seller_id)
    if status:query=query.filter_by(status=status)
    if q:query=query.filter(ImportedOrder.external_id.contains(q,autoescape=True))
    return {"results":[order_out(o) for o in query.order_by(ImportedOrder.created_at.desc(),ImportedOrder.id.desc()).offset(offset).limit(limit)],"total":query.count(),"offset":offset,"limit":limit}
@router.get("/marketplace-orders/summary")
def summary(db:Session=Depends(get_db)):
    c=latest(db)
    if not c:return {"count":0,"paid_totals":{},"last_sync":None,"scope":"imported_snapshots"}
    query=db.query(ImportedOrder).filter_by(seller_id=c.seller_id)
    totals=db.query(ImportedOrder.currency,func.sum(ImportedOrder.total)).filter_by(seller_id=c.seller_id,status="paid").group_by(ImportedOrder.currency).all()
    run=db.query(SyncRun).filter_by(seller_id=c.seller_id).order_by(SyncRun.id.desc()).first()
    return {"count":query.count(),"paid_totals":{currency:str(amount(total)) for currency,total in totals},
        "last_sync":{"status":run.status,"count":run.count,"days":run.days,"finished_at":run.finished_at,"note":run.note} if run else None,"scope":"imported_snapshots"}
@router.get("/sync-runs")
def sync_runs(db:Session=Depends(get_db)):
    c=latest(db)
    if not c:return []
    return [{"id":r.id,"status":r.status,"count":r.count,"days":r.days,"started_at":r.started_at,"finished_at":r.finished_at,"note":r.note} for r in db.query(SyncRun).filter_by(seller_id=c.seller_id).order_by(SyncRun.id.desc()).limit(30)]
@router.get("/reports/marketplace-orders.csv")
def report(db:Session=Depends(get_db)):
    c=latest(db);out=io.StringIO();w=csv.writer(out,delimiter=";")
    w.writerow(["Pedido ML","Status","Moeda","Total bruto","Criado em","Importado em"])
    if c:
        for o in db.query(ImportedOrder).filter_by(seller_id=c.seller_id).order_by(ImportedOrder.created_at.desc()):
            def safe(value):
                s=str(value)
                return "'"+s if s.startswith(("=","+","-","@","\t","\r","\n")) else s
            w.writerow([safe(v) for v in (o.external_id,o.status,o.currency,o.total,o.created_at.isoformat(),o.imported_at.isoformat())])
    return Response("\ufeff"+out.getvalue(),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=pedidos-mercadolivre.csv"})
