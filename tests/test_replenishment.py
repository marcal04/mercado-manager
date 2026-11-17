from datetime import timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import MarketplaceConnection,Product,now
from app.operations import Inventory,Economics
from app.replenishment import Policy
from app.marketplace_orders import ImportedOrder,SyncRun

def setup(c,stock=5,cost=10):
    p=c.post("/api/v1/products",json={"sku":"meli:MLB1","name":"Produto reposição","brand":"Test","reference_price":30,"stock":99}).json()["id"]
    c.put(f"/api/v1/products/{p}/economics",json={"cost":cost,"fee_percent":10,"shipping":0,"tax_percent":0,"minimum_margin":20,"minimum_stock":3})
    if stock is not None:
        c.post(f"/api/v1/products/{p}/stock",json={"delta":stock,"reason":"Contagem inicial","actor":"Operador"})
    return p

def policy(c,p,daily="2"):
    return c.put(f"/api/v1/replenishment/products/{p}/policy",json={"supplier":"Fornecedor teste","lead_days":3,"coverage_days":7,"safety_units":4,"pack_size":6,"minimum_order":10,"incoming_units":2,"daily_demand":daily,"actor":"Operador"})
def row(c):return c.get("/api/v1/replenishment/plan").json()["results"][0]

def test_no_assumed_inventory_or_demand():
    with TestClient(app) as c:
        p=setup(c,stock=None);r=row(c)
        assert r["physical_stock"] is None and r["daily_demand"] is None
        assert r["suggested_quantity"] is None and not r["can_propose"]
        assert len(r["blockers"])==3
        policy(c,p);r=row(c);assert not r["can_propose"]
        c.post(f"/api/v1/products/{p}/stock",json={"delta":0,"reason":"Contagem zero","actor":"Operador"})
        assert row(c)["can_propose"]

def test_pack_minimum_reserved_and_incoming():
    with TestClient(app) as c:
        p=setup(c);assert policy(c,p).status_code==200
        c.post("/api/v1/orders",json={"reference":"Reserva manual","product_id":p,"quantity":1,"unit_price":30})
        r=row(c)
        assert r["manual_pending_units"]==1 and r["available_with_incoming"]==6
        assert r["target_stock"]==24 and r["suggested_quantity"]==18
        assert r["estimated_cost"]=="180.00" and r["coverage_days"]==3
        assert r["demand_basis"]=="manual"
        # Once confirmed, the same units are already deducted from physical stock, never reserved twice.
        o=c.get("/api/v1/orders").json()[0]
        c.post(f'/api/v1/orders/{o["id"]}/status',json={"status":"confirmed","actor":"Operador"})
        r=row(c);assert r["manual_pending_units"]==0 and r["available_with_incoming"]==6

def test_proposal_simulated_duplicate_and_stale():
    with TestClient(app) as c:
        p=setup(c);policy(c,p);r=row(c);url=f"/api/v1/replenishment/products/{p}/proposal"
        body={"actor":"Operador","fingerprint":r["fingerprint"]}
        response=c.post(url,json=body);assert response.status_code==201
        assert c.post(url,json=body).status_code==409
        a=c.get("/api/v1/approvals").json()[0]
        assert a["payload"]["plan"]["suggested_quantity"]==18 and a["payload"]["proposal"]["risks"]
        c.post(f'/api/v1/approvals/{a["id"]}/decision',json={"approved":True,"actor":"Operador"})
        assert row(c)["physical_stock"]==5
        assert c.get("/api/v1/catalog").json()[0]["marketplace_stock"]==99
        c.post(f"/api/v1/products/{p}/stock",json={"delta":1,"reason":"Nova contagem","actor":"Operador"})
        assert c.post(url,json=body).status_code==409

def seed_history():
    end=now()
    with SessionLocal() as db:
        db.add(MarketplaceConnection(seller_id="123",access_token_encrypted="unused",refresh_token_encrypted="unused",expires_at=end))
        db.add(SyncRun(seller_id="123",status="success",days=30,count=5,started_at=end,finished_at=end))
        for id,status,qty,age,seller in [(1,"paid",30,1,"123"),(2,"paid",15,2,"123"),(3,"cancelled",999,1,"123"),(4,"paid",999,31,"123"),(5,"paid",999,1,"999")]:
            db.add(ImportedOrder(seller_id=seller,external_id=str(id),status=status,currency="BRL",total=100,created_at=end-timedelta(days=age),updated_at=end,imported_at=end,items=[{"item_id":"MLB1","variation_id":str(id),"quantity":qty}]))
        db.commit()

def test_paid_history_window_account_and_variations():
    with TestClient(app) as c:
        p=setup(c);policy(c,p,daily=None);seed_history();r=row(c)
        assert r["history_quality"]=="ready" and r["sold_units"]==45
        assert r["daily_demand"]=="1.5000" and r["target_stock"]==19
        # A wider requested window cannot reuse a shorter sync as complete evidence.
        r=c.get("/api/v1/replenishment/plan?days=90").json()["results"][0]
        assert r["daily_demand"] is None and not r["can_propose"]

def test_partial_or_stale_history_is_not_zero_demand():
    with TestClient(app) as c:
        p=setup(c);policy(c,p,daily=None);seed_history()
        with SessionLocal() as db:
            sync=db.query(SyncRun).one();sync.status="partial";db.commit()
        r=row(c);assert r["daily_demand"] is None and not r["can_propose"]
        with SessionLocal() as db:
            sync=db.query(SyncRun).one();sync.status="success";sync.finished_at=now()-timedelta(hours=25);db.commit()
        assert row(c)["history_quality"]=="stale_history"
        policy(c,p,daily="2");assert row(c)["can_propose"] and row(c)["demand_basis"]=="manual"

def test_zero_cost_no_restock_and_invalid_rules():
    with TestClient(app) as c:
        p=setup(c,stock=100,cost=0);policy(c,p,daily="0");r=row(c)
        assert r["suggested_quantity"]==0 and not r["can_propose"]
        assert not r["blockers"]  # A confirmed zero cost is valid.
        assert c.put(f"/api/v1/replenishment/products/{p}/policy",json={"supplier":"Test","lead_days":0,"coverage_days":10,"safety_units":0,"pack_size":0,"minimum_order":1,"incoming_units":0,"actor":"Test"}).status_code==422
