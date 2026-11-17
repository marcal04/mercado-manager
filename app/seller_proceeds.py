"""What remains after API-reported platform charges, without inventing taxes."""
import json
import re
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.orm import Mapped, Session, mapped_column
from starlette.concurrency import run_in_threadpool
from .db import Base, get_db
from .models import Product, now
from .data_hub import Snapshot, upsert_snapshot
from .marketplace_orders import latest
from .security import decrypt
from .services.mercadolivre import MercadoLivreClient, API_URL
from .operations import Economics, audit
from .stock_profile import StockProfile
from .auth_guard import limited

router = APIRouter(prefix="/api/v1/seller-proceeds")


class SellerGoal(Base):
    __tablename__ = "seller_margin_goals"
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    minimum_margin: Mapped[Decimal] = mapped_column(Numeric(6, 2))


class GoalInput(BaseModel):
    minimum_margin: Decimal = Field(ge=0, le=99, decimal_places=2)


class QuoteInput(BaseModel):
    product_id: int = Field(gt=0)
    price: Decimal | None = Field(default=None, gt=0, le=10000000, decimal_places=2)


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def money(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if value is not None else None


def context(item, price):
    shipping = item.get("shipping") or {}
    return {"price": money(price), "currency": item.get("currency_id"), "listing_type_id": item.get("listing_type_id"),
            "category_id": item.get("category_id"), "shipping_mode": shipping.get("mode"),
            "logistic_type": shipping.get("logistic_type"), "free_shipping": shipping.get("free_shipping"),
            "condition": item.get("condition")}


def fee_params(item, price):
    shipping = item.get("shipping") or {}
    return {k: v for k, v in {"price": money(price), "listing_type_id": item.get("listing_type_id"),
        "category_id": item.get("category_id"), "currency_id": item.get("currency_id"),
        "catalog_product_id": item.get("catalog_product_id"), "shipping_mode": shipping.get("mode"),
        "logistic_type": shipping.get("logistic_type")}.items() if v is not None}


def shipping_params(item, price):
    shipping = item.get("shipping") or {}
    return {k: v for k, v in {"item_id": item["id"], "item_price": money(price),
        "listing_type_id": item.get("listing_type_id"), "mode": shipping.get("mode"),
        "logistic_type": shipping.get("logistic_type"), "condition": item.get("condition"),
        "free_shipping": str(shipping["free_shipping"]).lower() if isinstance(shipping.get("free_shipping"), bool) else None,
        "verbose": "true"}.items() if v is not None}


def annotate(payload, quote_context):
    if isinstance(payload, dict):
        return {**payload, "_integration_quote_context": quote_context}
    if isinstance(payload, list):
        return [{**p, "_integration_quote_context": quote_context} for p in payload if isinstance(p, dict)]
    return payload


def read(row):
    return json.loads(decrypt(row.payload_encrypted)) if row else None


def result(db, product, payload, stamp):
    profile = db.get(StockProfile, product.id)
    economics = db.get(Economics, product.id)
    cost = profile.acquisition_cost if profile and profile.acquisition_cost is not None else economics.cost if economics else None
    goal = db.get(SellerGoal, product.id)
    target = goal.minimum_margin if goal else economics.minimum_margin if economics else None
    price = number(payload.get("price")); currency = payload.get("currency")
    issues = list(payload.get("issues") or [])
    fee_payload = payload.get("fee")
    choices = fee_payload if isinstance(fee_payload, list) else [fee_payload] if isinstance(fee_payload, dict) else []
    fee = next((f for f in choices if isinstance(f, dict) and f.get("listing_type_id") == payload.get("context", {}).get("listing_type_id")), None)
    commission = number(fee.get("sale_fee_amount")) if fee and fee.get("currency_id") == currency else None
    coverage = ((payload.get("shipping") or {}).get("coverage") or {}).get("all_country") or {}
    shipping = number(coverage.get("list_cost")) if coverage.get("currency_id") == currency else None
    if commission is None: issues.append("A tarifa total não foi informada na moeda do anúncio.")
    if shipping is None: issues.append("A cotação de frete do vendedor não foi informada na moeda do anúncio.")
    if cost is None: issues.append("Custo de aquisição ainda não confirmado.")
    stamp = stamp.replace(tzinfo=timezone.utc) if stamp and stamp.tzinfo is None else stamp
    if not stamp or now()-stamp > timedelta(hours=24): issues.append("Cotação antiga ou ausente; atualize para calcular.")
    if price is None or price <= 0 or not currency: issues.append("Preço ou moeda não disponível.")
    subtotal = price-commission-shipping if price is not None and commission is not None and shipping is not None else None
    remaining = subtotal-cost if subtotal is not None and cost is not None and not issues else None
    margin = remaining/price*100 if remaining is not None and price else None
    return {"product_id": product.id, "name": product.name, "price": money(price), "currency": currency,
            "sale_fee": money(commission), "seller_shipping_estimate": money(shipping), "acquisition_cost": money(cost),
            "platform_charges_estimate": money(commission+shipping) if commission is not None and shipping is not None else None,
            "remaining_estimate": money(remaining), "remaining_margin_percent": money(margin), "minimum_margin": money(target),
            "meets_target": margin >= target if margin is not None and target is not None else None,
            "fetched_at": stamp, "state": "estimated" if remaining is not None else "incomplete",
            "issues": list(dict.fromkeys(issues)), "seller_tax_withholding": None,
            "notice": "Estimativa para uma unidade, após tarifa total, frete cotado e aquisição. A tarifa fixa já integra a tarifa total. Retenções, descontos e ajustes não informados nesta cotação não foram presumidos nem descontados novamente.",
            "source": "Mercado Livre API", "bank_settlement_verified": False}


def cached_results(db):
    connection = latest(db)
    rows = []
    from .catalog_sync import products_query
    for product in products_query(db).order_by(Product.id):
        identity = product.sku[5:] if product.sku.startswith("meli:") else None
        snapshot = db.query(Snapshot).filter_by(seller_id=connection.seller_id, module="proceeds_quotes",
            external_id=f"{identity}:{money(Decimal(str(product.reference_price)))}").first() if connection and identity else None
        sources = {}
        candidate = read(snapshot) if snapshot else None
        stamp = snapshot.fetched_at if snapshot else None
        if connection and identity:
            sources = {r.module:r for r in db.query(Snapshot).filter(Snapshot.seller_id==connection.seller_id,
                Snapshot.external_id==identity, Snapshot.module.in_(["listings","fees","shipping_quotes"]))}
            item = read(sources.get("listings"))
            fee = read(sources.get("fees")); shipping = read(sources.get("shipping_quotes"))
            if item and fee and shipping:
                expected = context(item, Decimal(str(product.reference_price)))
                fee_rows = fee if isinstance(fee,list) else [fee]
                fee_context = next((f.get("_integration_quote_context") for f in fee_rows if isinstance(f,dict) and f.get("listing_type_id")==item.get("listing_type_id")),None)
                if fee_context==expected and shipping.get("_integration_quote_context")==expected:
                    source_stamp = min(sources["fees"].fetched_at, sources["shipping_quotes"].fetched_at)
                    if not stamp or source_stamp>stamp:
                        candidate={"price":product.reference_price,"currency":item.get("currency_id"),"context":expected,"fee":fee,"shipping":shipping,"issues":[]}
                        stamp=source_stamp
        if candidate and sources.get("listings"):
            current_item = read(sources["listings"])
            if current_item.get("price") is not None and (number(current_item["price"]) != number(candidate.get("price")) or context(current_item, number(candidate.get("price"))) != candidate.get("context")):
                candidate = None
        if candidate:
            rows.append(result(db, product, candidate, stamp))
        else:
            rows.append(result(db, product, {"price": product.reference_price, "issues": ["Atualize a cotação automática deste anúncio."]}, None))
    return rows


async def collect_quote(db, product_id, proposed_price=None):
    product = db.get(Product, product_id)
    connection = latest(db)
    if not product or not connection or not re.fullmatch(r"meli:[A-Z]{3}\d+", product.sku):
        raise HTTPException(409, "Selecione um anúncio da conta conectada.")
    identity = product.sku[5:]
    headers = await MercadoLivreClient(db).authorization_headers(connection.id)
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        try:
            response = await client.get(API_URL+"/items/"+identity, headers=headers)
            response.raise_for_status(); item = response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "Não foi possível consultar o anúncio no Mercado Livre.") from None
        if not isinstance(item,dict):raise HTTPException(502,"Resposta inesperada ao consultar o anúncio.")
        if str(item.get("seller_id")) != connection.seller_id:
            raise HTTPException(403, "Anúncio não pertence à conta conectada.")
        actual_price = number(item.get("price"))
        price = proposed_price if proposed_price is not None else actual_price
        if actual_price is None or actual_price<=0 or price is None or price <= 0: raise HTTPException(409, "O anúncio não tem preço disponível para cotação.")
        quote_context = context(item, price)
        cache_key = f"{identity}:{money(price)}"
        previous = db.query(Snapshot).filter_by(seller_id=connection.seller_id, module="proceeds_quotes", external_id=cache_key).first()
        if previous:
            stamp = previous.fetched_at.replace(tzinfo=timezone.utc) if previous.fetched_at.tzinfo is None else previous.fetched_at
            value = read(previous)
            if now()-stamp < timedelta(seconds=30) and value.get("context") == quote_context:
                return result(db, product, value, stamp)
        payload = {"price": money(price), "currency": item.get("currency_id"), "context": quote_context, "issues": []}
        for name, path, params in (("fee", f"/sites/{item.get('site_id','MLB')}/listing_prices", fee_params(item, price)),
                                  ("shipping", f"/users/{connection.seller_id}/shipping_options/free", shipping_params(item, price))):
            if name == "shipping" and not isinstance((item.get("shipping") or {}).get("free_shipping"), bool):
                payload["issues"].append("Anúncio sem indicação da modalidade de frete.");continue
            try:
                response = await client.get(API_URL+path, params=params, headers=headers)
                response.raise_for_status(); value=response.json()
                if not isinstance(value,dict) and not (name=="fee" and isinstance(value,list)):raise ValueError("Unexpected quote shape")
                payload[name] = annotate(value, quote_context)
            except (httpx.HTTPError, ValueError):
                payload["issues"].append("O Mercado Livre não disponibilizou a cotação de "+("tarifa." if name == "fee" else "frete."))
        product.reference_price = actual_price
        upsert_snapshot(db, connection.seller_id, "listings", identity, "/items/"+identity, item)
        upsert_snapshot(db, connection.seller_id, "proceeds_quotes", cache_key, "listing_prices+shipping_options/free", payload)
        if price == actual_price:
            for name, module, path in (("fee", "fees", f"/sites/{item.get('site_id','MLB')}/listing_prices"), ("shipping", "shipping_quotes", f"/users/{connection.seller_id}/shipping_options/free")):
                if payload.get(name) is not None: upsert_snapshot(db, connection.seller_id, module, identity, path, payload[name])
        db.commit()
        return result(db, product, payload, now())


@router.get("")
def get_results(db: Session = Depends(get_db)):
    return cached_results(db)


@router.post("/quote")
async def quote(body: QuoteInput, request: Request, db: Session = Depends(get_db)):
    if await run_in_threadpool(limited, request, "proceeds"):
        raise HTTPException(429, "Aguarde um minuto antes de consultar novas cotações.")
    return await collect_quote(db, body.product_id, body.price)


@router.put("/products/{product_id}/goal")
def save_goal(product_id: int, body: GoalInput, db: Session = Depends(get_db)):
    if not db.get(Product, product_id): raise HTTPException(404, "Produto não encontrado.")
    goal = db.get(SellerGoal, product_id)
    if not goal: goal = SellerGoal(product_id=product_id);db.add(goal)
    goal.minimum_margin = body.minimum_margin
    audit(db, "seller_margin.updated", "product", product_id, body.model_dump(mode="json"));db.commit()
    return {"saved": True, "minimum_margin": money(body.minimum_margin)}
