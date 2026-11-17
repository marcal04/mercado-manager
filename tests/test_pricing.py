from decimal import Decimal
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import Approval, Product
from app.pricing import PriceSimulation

def setup_product(c,costs=True):
    p=c.post("/api/v1/products",json={"sku":"PRICING","name":"Produto de teste","brand":"Test","reference_price":200,"stock":5}).json()["id"]
    if costs:
        assert c.put(f"/api/v1/products/{p}/economics",json={"cost":"100.00","fee_percent":"10.00","shipping":"10.00","tax_percent":"5.00","minimum_margin":"20.00","minimum_stock":2}).status_code==200
    return p

def test_floor_rounds_up_and_matches_target():
    with TestClient(app) as c:
        p=setup_product(c)
        r=c.post("/api/v1/pricing/simulate",json={"product_id":p,"proposed_price":"169.23"})
        assert r.status_code==201
        sim=r.json();assert sim["minimum_price"]=="169.24" and sim["break_even_price"]=="129.42"
        assert sim["max_discount_percent"]=="15.38"
        rows={s["key"]:s for s in sim["scenarios"]}
        assert rows["current"]["profit"]=="60.00" and rows["current"]["margin"]=="30.00"
        assert rows["minimum"]["meets_target"] is True
        assert rows["custom"]["meets_target"] is False
        # The rounded displayed margin can equal the target; the decision uses unrounded math.
        assert c.post(f'/api/v1/pricing/simulations/{sim["id"]}/proposal',json={"scenario":"custom","actor":"Operador"}).status_code==409
        assert c.get("/api/v1/catalog").json()[0]["economics"]["target_price"]==169.24

def test_manual_reference_never_pushes_price_below_floor():
    with TestClient(app) as c:
        p=setup_product(c)
        sim=c.post("/api/v1/pricing/simulate",json={"product_id":p,"competitor_price":"100.00","undercut_percent":"1"}).json()
        row=next(s for s in sim["scenarios"] if s["key"]=="competitive")
        assert row["price"]==sim["minimum_price"] and row["meets_target"]
        sim=c.post("/api/v1/pricing/simulate",json={"product_id":p,"competitor_price":"190.00","undercut_percent":"1"}).json()
        row=next(s for s in sim["scenarios"] if s["key"]=="competitive")
        assert row["price"]=="188.10"

def test_proposal_is_reviewable_and_does_not_change_product():
    with TestClient(app) as c:
        p=setup_product(c)
        sim=c.post("/api/v1/pricing/simulate",json={"product_id":p}).json()
        url=f'/api/v1/pricing/simulations/{sim["id"]}/proposal'
        assert c.post(url,json={"scenario":"current","actor":"Operador"}).status_code==409
        r=c.post(url,json={"scenario":"discount5","actor":"Operador"});assert r.status_code==201
        approval_id=r.json()["approval_id"]
        assert c.post(url,json={"scenario":"discount5","actor":"Operador"}).status_code==409
        a=c.get("/api/v1/approvals").json()[0]
        assert a["payload"]["calculation"]["assumptions"]["cost"]=="100.00"
        assert a["payload"]["proposal"]["benefits"] and a["payload"]["proposal"]["risks"]
        c.post(f"/api/v1/approvals/{approval_id}/decision",json={"approved":True,"actor":"Operador"})
        prod=c.get("/api/v1/catalog").json()[0]
        assert prod["price"]==200 and prod["marketplace_stock"]==5 and prod["local_stock"] is None

def test_changed_costs_require_fresh_simulation():
    with TestClient(app) as c:
        p=setup_product(c)
        sim=c.post("/api/v1/pricing/simulate",json={"product_id":p}).json()
        c.put(f"/api/v1/products/{p}/economics",json={"cost":"150.00","fee_percent":"10.00","shipping":"10.00","tax_percent":"5.00","minimum_margin":"20.00","minimum_stock":2})
        r=c.post(f'/api/v1/pricing/simulations/{sim["id"]}/proposal',json={"scenario":"discount5","actor":"Operador"})
        assert r.status_code==409
        with SessionLocal() as db: assert db.query(Approval).count()==0
        history=c.get("/api/v1/pricing/simulations").json()
        assert history[0]["assumptions"]["cost"]=="100.00"

def test_missing_costs_and_invalid_values_never_persist():
    with TestClient(app) as c:
        p=setup_product(c,False)
        assert c.post("/api/v1/pricing/simulate",json={"product_id":p}).status_code==409
        for bad in ["-1","0","NaN","Infinity","1.123"]:
            assert c.post("/api/v1/pricing/simulate",json={"product_id":p,"proposed_price":bad}).status_code==422
        assert c.post("/api/v1/pricing/simulate",json={"product_id":p,"undercut_percent":21}).status_code==422
        with SessionLocal() as db:assert db.query(PriceSimulation).count()==0
