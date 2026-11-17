import sys
from types import SimpleNamespace
from fastapi.testclient import TestClient
from app.main import app
from app.config import get_settings

def new_product(c):
    r=c.post("/api/v1/products",json={"sku":"ONE","name":"Test","brand":"Brand","reference_price":200,"stock":99})
    assert r.status_code==201
    return r.json()["id"]

def test_economics_and_validation():
    with TestClient(app) as c:
        p=new_product(c)
        costs={"cost":"100.00","fee_percent":"10.00","shipping":"10.00","tax_percent":"5.00","minimum_margin":"20.00","minimum_stock":2}
        assert c.put(f"/api/v1/products/{p}/economics",json=costs).status_code==200
        e=c.get("/api/v1/catalog").json()[0]["economics"]
        assert e["profit"]==60 and e["margin"]==30 and e["target_price"]==169.24
        costs["minimum_margin"]="90"
        assert c.put(f"/api/v1/products/{p}/economics",json=costs).status_code==422
        assert c.get("/api/v1/catalog").json()[0]["economics"]["minimum_margin"]==20

def test_order_stock_atomicity_and_cancellation():
    with TestClient(app) as c:
        p=new_product(c)
        assert c.post(f"/api/v1/products/{p}/stock",json={"delta":5,"reason":"Contagem física","actor":"Operador"}).status_code==200
        body={"reference":"A1","product_id":p,"quantity":6,"unit_price":"200.00"}
        o=c.post("/api/v1/orders",json=body).json()
        def transition(id,status): return c.post(f"/api/v1/orders/{id}/status",json={"status":status,"actor":"Operador"})
        assert transition(o["id"],"confirmed").status_code==409
        assert c.get("/api/v1/orders").json()[0]["status"]=="pending"
        body.update(reference="A2",quantity=2)
        o=c.post("/api/v1/orders",json=body).json()
        assert transition(o["id"],"confirmed").status_code==200
        assert transition(o["id"],"confirmed").status_code==409
        assert c.get("/api/v1/catalog").json()[0]["local_stock"]==3
        assert transition(o["id"],"cancelled").status_code==200
        assert c.get("/api/v1/catalog").json()[0]["local_stock"]==5
        assert c.get("/api/v1/catalog").json()[0]["marketplace_stock"]==99
        assert c.get("/api/v1/overview").json()["local_order_revenue"]==0
        assert c.post("/api/v1/orders",json=body).status_code==409
        assert len(c.get("/api/v1/stock-movements").json())==3

def test_shipped_cannot_cancel_and_revenue():
    with TestClient(app) as c:
        p=new_product(c)
        c.post(f"/api/v1/products/{p}/stock",json={"delta":5,"reason":"Inicial","actor":"Operador"})
        o=c.post("/api/v1/orders",json={"reference":"S1","product_id":p,"quantity":1,"unit_price":"200.00"}).json()
        for status in ["confirmed","shipped"]:
            assert c.post(f'/api/v1/orders/{o["id"]}/status',json={"status":status,"actor":"Operador"}).status_code==200
        assert c.post(f'/api/v1/orders/{o["id"]}/status',json={"status":"cancelled","actor":"Operador"}).status_code==409
        assert c.get("/api/v1/overview").json()["local_order_revenue"]==200

def test_campaign_approval_is_simulation_and_decision_once():
    with TestClient(app) as c:
        p=new_product(c)
        r=c.post("/api/v1/campaigns",json={"name":"Campanha teste","product_id":p,"budget":"150.00","objective":"Avaliar demanda"})
        assert r.status_code==201
        a=r.json()["approval_id"]
        assert c.post(f"/api/v1/approvals/{a}/decision",json={"approved":True,"actor":"Operador"}).status_code==200
        assert c.post(f"/api/v1/approvals/{a}/decision",json={"approved":False,"actor":"Operador"}).status_code==409
        campaign=c.get("/api/v1/campaigns").json()[0]
        assert campaign["status"]=="approved" and campaign["simulated"] is True
        assert c.get("/api/v1/catalog").json()[0]["marketplace_stock"]==99

def test_paid_ai_never_called_when_disabled(monkeypatch):
    def forbidden(*args,**kwargs): raise AssertionError("Paid AI must not be called")
    monkeypatch.setenv("OPENAI_API_KEY","test-only-placeholder")
    monkeypatch.setitem(sys.modules,"openai",SimpleNamespace(OpenAI=forbidden))
    get_settings.cache_clear()
    with TestClient(app) as c:
        assert c.post("/api/v1/chat",json={"message":"Olá"}).status_code==200
        assert c.post("/api/v1/chat",json={"message":"Sugira um preço"}).json()["approval_id"]

def test_access_and_csv_safety(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_TOKEN","test-password-only")
    get_settings.cache_clear()
    with TestClient(app) as c:
        assert c.get("/api/v1/catalog").status_code==401
        assert c.get("/health").status_code==200
        assert c.post("/session",json={"password":"wrong"}).status_code==401
        assert c.post("/session",json={"password":"test-password-only"}).status_code==200
        assert c.post("/api/v1/products",headers={"origin":"https://foreign.example"},json={}).status_code==403
        assert c.post("/api/v1/products",json={"sku":"=1+1","name":"@bad","brand":"Test","reference_price":100,"stock":0}).status_code==201
        report=c.get("/api/v1/reports/catalog.csv")
        assert report.status_code==200 and "'=1+1" in report.text and "'@bad" in report.text
        assert c.delete("/session").status_code==200
        assert c.get("/api/v1/catalog").status_code==401


def test_price_proposal_requires_costs_and_keeps_marketplace_unchanged():
    with TestClient(app) as c:
        p=new_product(c)
        body={"proposed_price":"90.00","reason":"Teste de margem"}
        assert c.post(f"/api/v1/products/{p}/price-proposals",json=body).status_code==409
        assert c.put(f"/api/v1/products/{p}/economics",json={"cost":"100.00","fee_percent":"10.00","shipping":"0.00","tax_percent":"0.00","minimum_margin":"20.00","minimum_stock":2}).status_code==200
        r=c.post(f"/api/v1/products/{p}/price-proposals",json=body)
        assert r.status_code==201
        assert len(r.json()["proposal"]["risks"])==2
        a=r.json()["id"]
        c.post(f"/api/v1/approvals/{a}/decision",json={"approved":True,"actor":"Operador"})
        assert c.get("/api/v1/catalog").json()[0]["price"]==200
