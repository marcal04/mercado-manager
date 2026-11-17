"""Account-scoped daily overview. Missing history never becomes a confirmed zero."""
from datetime import timedelta, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from .db import get_db
from .models import Product, now
from .marketplace_orders import latest, ImportedOrder, SyncRun, order_out
from .data_hub import DataJob, Snapshot
from .operations import Economics
from .stock_profile import StockProfile
from .auto_sync import AutoSync
from .maintenance import backup_status
from .access import protected_access_configured

router = APIRouter(prefix="/api/v1")


def aware(stamp):
    return stamp.replace(tzinfo=timezone.utc) if stamp and stamp.tzinfo is None else stamp


@router.get("/store-dashboard")
def dashboard(days: int = Query(default=30, ge=1, le=365), db: Session = Depends(get_db)):
    connection = latest(db)
    seller = connection.seller_id if connection else None
    run = db.query(SyncRun).filter_by(seller_id=seller).order_by(SyncRun.id.desc()).first() if seller else None
    job = db.query(DataJob).filter_by(seller_id=seller).order_by(DataJob.id.desc()).first() if seller else None
    cutoff = aware(run.finished_at) if run and run.finished_at else None
    end = cutoff or now()
    start = end-timedelta(days=days)
    quality = "missing"
    if cutoff:
        quality = "ready" if run.status == "success" else "partial"
        if run.days < days:
            quality = "insufficient_window"
        if now()-cutoff > timedelta(hours=24):
            quality = "stale"
    orders_progress = (job.progress or {}).get("orders", {}) if job else {}
    if job and (not cutoff or aware(job.started_at) >= cutoff) and orders_progress.get("status") in {"failed", "denied", "unavailable", "blocked", "partial"}:
        quality = "failed_latest_attempt"
    query = db.query(ImportedOrder).filter(ImportedOrder.seller_id == seller,
                ImportedOrder.created_at >= start, ImportedOrder.created_at <= end) if seller else None
    count = query.count() if query is not None else 0
    grouped = dict(query.with_entities(ImportedOrder.status, func.count(ImportedOrder.id)).group_by(ImportedOrder.status).all()) if query is not None else {}
    paid = query.filter(ImportedOrder.status == "paid") if query is not None else None
    totals = paid.with_entities(ImportedOrder.currency, func.sum(ImportedOrder.total)).group_by(ImportedOrder.currency).all() if paid is not None else []
    gross = {currency: str(Decimal(value).quantize(Decimal("0.01"))) for currency, value in totals}
    complete = quality == "ready"
    from .catalog_sync import products_query
    products = products_query(db).order_by(Product.id).all()
    profiles = {p.product_id: p for p in db.query(StockProfile)}
    expenses = {e.product_id: e for e in db.query(Economics)}
    acquisition = sum(1 for p in products if (profiles.get(p.id) and profiles[p.id].acquisition_cost is not None) or p.id in expenses)
    from .seller_proceeds import cached_results
    automatic_quotes = sum(r["state"] == "estimated" for r in cached_results(db))
    snapshots = db.query(Snapshot).filter_by(seller_id=seller, module="listings").all() if seller else []
    by_item = {s.external_id: s for s in snapshots}
    available = [p for p in products if p.sku.startswith("meli:") and p.sku[5:] in by_item]
    stock_fresh = bool(available) and all(now()-aware(by_item[p.sku[5:]].fetched_at) <= timedelta(hours=24) for p in available)
    automatic = db.get(AutoSync, seller) if seller else None
    backup = backup_status()
    checks = []

    def check(code, label, state, detail, target, product_id=None):
        checks.append({"code": code, "label": label, "state": state, "detail": detail,
                       "target": target, "product_id": product_id})

    check("account", "Conta do Mercado Livre", "ready" if seller else "pending",
          "Conta cadastrada; a sincronização verifica as permissões disponíveis." if seller else "Conecte sua conta para consultar a operação.", "settings")
    check("history", "Histórico de vendas", "ready" if complete else "pending",
          f"Janela de {days} dias conferida até a última importação." if complete else "A janela ainda precisa de uma importação completa e recente.", "orders")
    check("automatic", "Atualização automática", "ready" if automatic and automatic.enabled else "pending",
          f"A cada {automatic.interval_minutes//60} hora(s), enquanto o serviço estiver ligado." if automatic and automatic.enabled else "Ative ou confira o agendamento da conta.", "datahub")
    check("acquisition", "Origem e aquisição", "ready" if products and acquisition == len(products) else "pending",
          f"{acquisition} de {len(products)} produto(s) com aquisição confirmada. Estoque próprio pode ter custo zero.", "catalog")
    check("expenses", "Quanto sobra após as cobranças", "ready" if products and automatic_quotes == len(products) else "pending",
          f"{automatic_quotes} de {len(products)} anúncio(s) com tarifa, frete e aquisição disponíveis para estimar quanto sobra.", "pricing")
    check("stock", "Estoque anunciado", "ready" if stock_fresh and len(available) == len(products) else "pending",
          f"{len(available)} de {len(products)} produto(s) com estoque importado. A contagem física é um controle separado.", "datahub")
    check("backup", "Backup dos dados", "ready" if backup["backup_state"] == "ok" else "pending",
          "Cópia local atualizada." if backup["backup_state"] == "ok" else "Confira a última cópia e o serviço de backup.", "settings")
    check("access", "Acesso privado", "ready" if protected_access_configured(db) else "pending",
          "Acesso de operador configurado." if protected_access_configured(db) else "Conclua a criação do seu acesso local.", "settings")
    restrictions = [{"area": key, "state": value.get("status")} for key, value in (job.progress or {}).items()
                    if isinstance(value, dict) and value.get("status") in {"denied", "unavailable", "blocked", "partial", "failed"}] if job else []
    return {"days": days, "from": start, "through": cutoff, "history_quality": quality,
            "confirmed_window": complete, "sync_running": bool(job and job.status == "running"),
            "order_count": count if count or complete else None, "paid_count": grouped.get("paid", 0) if count or complete else None,
            "status_counts": grouped, "gross_paid_by_currency": gross,
            "recent_orders": [order_out(o) for o in query.order_by(ImportedOrder.created_at.desc(), ImportedOrder.id.desc()).limit(5)] if query is not None else [],
            "products": len(products), "acquisition_confirmed": acquisition, "expenses_confirmed": len(expenses), "automatic_quotes": automatic_quotes,
            "stock_from_api": len(available), "stock_recent": stock_fresh,
            "checks": checks, "pending_checks": sum(c["state"] == "pending" for c in checks), "restrictions": restrictions,
            "notice": "Totais brutos de pedidos com status pago, separados por moeda. Não são lucro ou repasse recebido. Controles manuais permanecem separados."}
