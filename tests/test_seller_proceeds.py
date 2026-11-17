from datetime import timedelta
from decimal import Decimal
import asyncio
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import Product, MarketplaceConnection, now
from app.stock_profile import StockProfile
from app.seller_proceeds import result, context, annotate, cached_results, collect_quote, SellerGoal
from app.data_hub import upsert_snapshot
from app.config import get_settings
from app.security import encrypt

ITEM={"id":"MLB123", "seller_id":123,"site_id":"MLB","price":200,"currency_id":"BRL","listing_type_id":"gold_special","category_id":"MLB1","condition":"new","shipping":{"mode":"me2","logistic_type":"drop_off","free_shipping":True}}
FEE={"listing_type_id":"gold_special","currency_id":"BRL","sale_fee_amount":26,"sale_fee_details":{"fixed_fee":6,"percentage_fee":10}}
SHIP={"coverage":{"all_country":{"list_cost":24,"currency_id":"BRL"}}}

@pytest.fixture
def owned(monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY",Fernet.generate_key().decode());get_settings.cache_clear()
    with SessionLocal() as db:
        p=Product(sku="meli:MLB123",name="Owned",brand="Test",reference_price=200,stock=3);db.add(p);db.flush()
        db.add(StockProfile(product_id=p.id,mode="owned",acquisition_cost=0));db.add(SellerGoal(product_id=p.id,minimum_margin=50))
        db.add(MarketplaceConnection(seller_id="123",access_token_encrypted=encrypt("fake"),refresh_token_encrypted=encrypt("fake"),expires_at=now()+timedelta(hours=1)))
        db.commit();return p.id

def payload():return {"price":"200.00","currency":"BRL","context":context(ITEM,Decimal(200)),"fee":FEE,"shipping":SHIP,"issues":[]}

def test_zero_cost_and_total_fee_not_double_counted(owned):
    with SessionLocal() as db:
        r=result(db,db.get(Product,owned),payload(),now())
        assert r["sale_fee"]=="26.00" and r["remaining_estimate"]=="150.00"
        assert r["remaining_margin_percent"]=="75.00" and r["meets_target"] is True
        assert r["seller_tax_withholding"] is None and not r["bank_settlement_verified"]

@pytest.mark.parametrize("change",["fee","shipping","currency","old"])
def test_unavailable_or_stale_is_pending_not_zero(owned,change):
    p=payload();stamp=now()
    if change in {"fee","shipping"}:p.pop(change)
    if change=="currency":p["currency"]="USD"
    if change=="old":stamp-=timedelta(hours=25)
    with SessionLocal() as db:
        r=result(db,db.get(Product,owned),p,stamp)
        assert r["remaining_estimate"] is None and r["state"]=="incomplete" and r["issues"]

def test_sync_context_and_changed_listing_invalidate_estimate(owned):
    with SessionLocal() as db:
        ctx=context(ITEM,Decimal(200))
        for module,value in [("listings",ITEM),("fees",annotate(FEE,ctx)),("shipping_quotes",annotate(SHIP,ctx))]:upsert_snapshot(db,"123",module,"MLB123","/test",value)
        db.commit();assert cached_results(db)[0]["remaining_estimate"]=="150.00"
        from app.operations import overview
        assert not any("aquisição" in a["message"] or "meta" in a["message"] for a in overview(db)["alerts"])
        from app.store_dashboard import dashboard
        assert dashboard(30,db)["automatic_quotes"]==1
        upsert_snapshot(db,"123","listings","MLB123","/test",{**ITEM,"price":300});db.commit()
        assert cached_results(db)[0]["remaining_estimate"] is None

def test_goal_saved_without_manual_expenses(owned):
    with TestClient(app) as c:
        r=c.put(f"/api/v1/seller-proceeds/products/{owned}/goal",json={"minimum_margin":55})
        assert r.status_code==200 and r.json()["minimum_margin"]=="55.00"
        assert c.get("/api/v1/seller-proceeds").status_code==200

def test_simulation_uses_provider_parameters_and_preserves_actual_quotes(owned,monkeypatch):
    calls=[];original=httpx.AsyncClient
    def handler(request):
        calls.append(request)
        if request.url.path=="/items/MLB123":return httpx.Response(200,json=ITEM)
        if request.url.path.endswith("listing_prices"):return httpx.Response(200,json=FEE)
        return httpx.Response(200,json=SHIP)
    monkeypatch.setattr("app.seller_proceeds.httpx.AsyncClient",lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    with SessionLocal() as db:
        upsert_snapshot(db,"123","fees","MLB123","/test",{"original":True});db.commit()
        r=asyncio.run(collect_quote(db,owned,Decimal(250)))
        assert r["price"]=="250.00" and db.get(Product,owned).reference_price==200
        from app.seller_proceeds import read
        from app.data_hub import Snapshot
        assert read(db.query(Snapshot).filter_by(module="fees").one())=={"original":True}
        assert calls[-1].url.params["mode"]=="me2" and calls[-1].url.params["free_shipping"]=="true"
        assert calls[-1].url.params["condition"]=="new" and calls[-1].url.params["item_price"]=="250.00"
        assert calls[-2].url.params["shipping_mode"]=="me2"
        assert all(r.method=="GET" for r in calls)
        asyncio.run(collect_quote(db,owned,Decimal(250)))
        assert len(calls)==4 # a fresh ownership/context check, then cached quote

def test_foreign_listing_rejected(owned,monkeypatch):
    original=httpx.AsyncClient
    monkeypatch.setattr("app.seller_proceeds.httpx.AsyncClient",lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={**ITEM,"seller_id":999})),**kw))
    from fastapi import HTTPException
    with SessionLocal() as db,pytest.raises(HTTPException) as error:asyncio.run(collect_quote(db,owned))
    assert error.value.status_code==403

def test_route_requires_private_access(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED","true");get_settings.cache_clear()
    with TestClient(app) as c:
        assert c.get("/api/v1/seller-proceeds").status_code==401
        assert c.post("/api/v1/seller-proceeds/quote",json={"product_id":1}).status_code==401
