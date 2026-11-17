from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.stock_profile import StockProfile
from app.operations import Economics

def product(c):
    return c.post("/api/v1/products",json={"sku":"owned-1","name":"Apple próprio","brand":"Apple","reference_price":200,"stock":4}).json()["id"]

def costs(cost=0):
    return {"cost":cost,"fee_percent":13,"shipping":15,"tax_percent":0,"minimum_margin":20,"minimum_stock":0}

def test_owned_zero_is_confirmed_without_inventing_other_costs():
    with TestClient(app) as c:
        p=product(c)
        r=c.put(f"/api/v1/products/{p}/stock-profile",json={"mode":"owned"})
        assert r.status_code==200 and r.json()["acquisition_cost"]=="0.00"
        catalog=c.get("/api/v1/catalog").json()[0]
        assert catalog["stock_profile"]["mode"]=="owned" and catalog["economics"] is None
        assert c.post("/api/v1/pricing/simulate",json={"product_id":p}).status_code==409
    with SessionLocal() as db:
        assert db.get(Economics,p) is None and db.get(StockProfile,p).acquisition_cost==0

def test_owned_stock_has_no_supplier_or_purchase_proposal():
    with TestClient(app) as c:
        p=product(c)
        c.put(f"/api/v1/products/{p}/stock-profile",json={"mode":"owned"})
        row=c.get("/api/v1/replenishment/plan").json()["results"][0]
        assert row["stock_mode"]=="owned" and row["unit_cost"]=="0.00"
        assert not row["blockers"] and not row["can_propose"] and row["suggested_quantity"] is None
        assert c.post(f"/api/v1/replenishment/products/{p}/proposal",json={"fingerprint":row["fingerprint"],"actor":"Operador"}).status_code==409
        policy={"supplier":"Fornecedor","lead_days":3,"coverage_days":7,"safety_units":0,"pack_size":1,"minimum_order":1,"incoming_units":0,"actor":"Operador"}
        assert c.put(f"/api/v1/replenishment/products/{p}/policy",json=policy).status_code==409

def test_cost_confirmation_and_margin_work_with_zero_purchase_cost():
    with TestClient(app) as c:
        p=product(c)
        c.put(f"/api/v1/products/{p}/stock-profile",json={"mode":"owned"})
        assert c.put(f"/api/v1/products/{p}/economics",json=costs()).status_code==200
        catalog=c.get("/api/v1/catalog").json()[0]
        assert catalog["economics"]["profit"]==159
        result=c.post("/api/v1/pricing/simulate",json={"product_id":p})
        assert result.status_code==201
        assert result.json()["assumptions"]["cost"]=="0.00"
        assert c.put(f"/api/v1/products/{p}/economics",json=costs(99)).status_code==422
        assert c.get("/api/v1/catalog").json()[0]["economics"]["cost"]==0

def test_change_origin_updates_only_acquisition_and_preserves_selling_costs():
    with TestClient(app) as c:
        p=product(c)
        c.put(f"/api/v1/products/{p}/economics",json=costs(50))
        assert c.put(f"/api/v1/products/{p}/stock-profile",json={"mode":"owned"}).status_code==200
        economics=c.get("/api/v1/catalog").json()[0]["economics"]
        assert economics["cost"]==0 and economics["shipping"]==15 and economics["fee_percent"]==13
        assert c.put(f"/api/v1/products/{p}/stock-profile",json={"mode":"resale"}).status_code==422
        assert c.put(f"/api/v1/products/{p}/stock-profile",json={"mode":"resale","acquisition_cost":40}).status_code==200
        catalog=c.get("/api/v1/catalog").json()[0]
        assert catalog["economics"]["cost"]==40 and catalog["stock_profile"]["replenishment_applicable"]

def test_invalid_origins_cannot_change_catalog():
    with TestClient(app) as c:
        p=product(c)
        assert c.put(f"/api/v1/products/{p}/stock-profile",json={"mode":"owned","acquisition_cost":10}).status_code==422
        assert c.put(f"/api/v1/products/{p}/stock-profile",json={"mode":"resale","acquisition_cost":-1}).status_code==422
        assert c.put(f"/api/v1/products/{p}/stock-profile",json={"mode":"unknown"}).status_code==422
        assert c.put("/api/v1/products/999/stock-profile",json={"mode":"owned"}).status_code==404
        assert c.get("/api/v1/catalog").json()[0]["stock_profile"] is None
