from datetime import timedelta
from decimal import Decimal
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import Product,MarketplaceConnection,now
from app.marketplace_orders import ImportedOrder,SyncRun
from app.data_hub import DataJob,upsert_snapshot
from app.stock_profile import StockProfile
from app.operations import Order
from app.security import encrypt
from app.config import get_settings

@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY",Fernet.generate_key().decode());get_settings.cache_clear()
    with SessionLocal() as db:
        db.add(MarketplaceConnection(seller_id="123",access_token_encrypted=encrypt("fake"),refresh_token_encrypted=encrypt("fake"),expires_at=now()+timedelta(hours=1)))
        db.commit()

def history(status="success",days=365,age=0):
    stamp=now()-timedelta(hours=age)
    with SessionLocal() as db:
        db.add(SyncRun(seller_id="123",status=status,days=days,count=0,started_at=stamp,finished_at=stamp,note="test"));db.commit()
    return stamp

def order(id,status="paid",currency="BRL",amount=100,seller="123",days=1):
    return ImportedOrder(seller_id=seller,external_id=str(id),status=status,currency=currency,total=Decimal(amount),created_at=now()-timedelta(days=days),updated_at=now(),imported_at=now(),items=[])

def test_missing_history_is_not_a_confirmed_zero():
    with TestClient(app) as c:
        d=c.get("/api/v1/store-dashboard").json()
        assert d["history_quality"]=="missing" and d["order_count"] is None
        assert d["paid_count"] is None and d["gross_paid_by_currency"]=={}
        assert not d["confirmed_window"]

def test_empty_complete_import_confirms_zero(connected):
    history()
    with TestClient(app) as c:
        d=c.get("/api/v1/store-dashboard").json()
        assert d["confirmed_window"] and d["order_count"]==d["paid_count"]==0
        assert d["gross_paid_by_currency"]=={} and d["through"]

def test_account_window_currency_and_local_orders_are_separate(connected):
    history()
    with SessionLocal() as db:
        p=Product(sku="local",name="Local",brand="Test",reference_price=50,stock=1);db.add(p);db.flush()
        db.add(Order(reference="manual",product_id=p.id,quantity=10,unit_price=Decimal(999),status="confirmed"))
        db.add_all([order(1),order(2,currency="USD",amount=20),order(3,status="cancelled",amount=300),order(4,seller="999",amount=5000),order(5,days=60)])
        db.commit()
    with TestClient(app) as c:
        d=c.get("/api/v1/store-dashboard?days=30").json()
        assert d["order_count"]==3 and d["paid_count"]==2
        assert d["gross_paid_by_currency"]=={"BRL":"100.00","USD":"20.00"}
        assert len(d["recent_orders"])==3 and d["status_counts"]["cancelled"]==1
        assert "manual" not in str(d["recent_orders"])
        assert c.get("/api/v1/store-dashboard?days=0").status_code==422

@pytest.mark.parametrize("status,days,age,quality",[("partial",365,0,"partial"),("success",7,0,"insufficient_window"),("success",365,27,"stale")])
def test_incomplete_history_is_not_silently_complete(connected,status,days,age,quality):
    history(status,days,age)
    with TestClient(app) as c:
        d=c.get("/api/v1/store-dashboard?days=30").json()
        assert d["history_quality"]==quality and not d["confirmed_window"] and d["order_count"] is None

def test_failed_new_attempt_marks_previous_cache_as_unconfirmed(connected):
    history()
    with SessionLocal() as db:
        db.add(DataJob(seller_id="123",status="completed_with_gaps",progress={"orders":{"status":"failed"}}));db.commit()
    with TestClient(app) as c:
        d=c.get("/api/v1/store-dashboard").json()
        assert d["history_quality"]=="failed_latest_attempt" and not d["confirmed_window"]

def test_owned_zero_and_api_stock_do_not_require_physical_count(connected):
    history()
    with SessionLocal() as db:
        p=Product(sku="meli:MLB1",name="Owned",brand="Test",reference_price=100,stock=3);db.add(p);db.flush()
        product_id=p.id
        db.add(StockProfile(product_id=p.id,mode="owned",acquisition_cost=0))
        upsert_snapshot(db,"123","listings","MLB1","/items/MLB1",{"available_quantity":3});db.commit()
    with TestClient(app) as c:
        d=c.get("/api/v1/store-dashboard").json();checks={x["code"]:x for x in d["checks"]}
        assert checks["acquisition"]["state"]==checks["stock"]["state"]=="ready"
        assert checks["expenses"]["state"]=="pending" and checks["expenses"]["target"]=="pricing"
        assert d["acquisition_confirmed"]==1 and d["expenses_confirmed"]==0
        assert all(x["code"]!="supplier" for x in d["checks"])

def test_dashboard_requires_authentication(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED","true");get_settings.cache_clear()
    with TestClient(app) as c:assert c.get("/api/v1/store-dashboard").status_code==401
