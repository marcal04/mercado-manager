from datetime import datetime,timedelta,timezone
from copy import deepcopy
from urllib.parse import parse_qs
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import MarketplaceConnection, Product
from app.operations import Inventory, Order
from app.marketplace_orders import ImportedOrder
from app.config import get_settings
from app.security import encrypt, decrypt

@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY",Fernet.generate_key().decode())
    monkeypatch.setenv("ML_CLIENT_ID","test-client")
    monkeypatch.setenv("ML_CLIENT_SECRET","test-secret")
    get_settings.cache_clear()
    with SessionLocal() as db:
        db.add(MarketplaceConnection(seller_id="123",access_token_encrypted=encrypt("fake-access"),refresh_token_encrypted=encrypt("fake-refresh"),expires_at=datetime.now(timezone.utc)+timedelta(hours=1)))
        p=Product(sku="meli:MLB1",name="Product",brand="Test",reference_price=100,stock=10);db.add(p);db.flush()
        db.add(Inventory(product_id=p.id,quantity=7));db.commit()

def order(id=1,status="paid",updated="2026-11-05T12:00:00Z"):
    return {"id":id,"seller":{"id":123},"status":status,"currency_id":"BRL","total_amount":100.25,
        "date_created":"2026-11-01T12:00:00-03:00","date_last_updated":updated,
        "buyer":{"name":"PRIVATE","email":"private@example.com"},"shipping":{"receiver_address":"PRIVATE ADDRESS"},
        "order_items":[{"item":{"id":"MLB1","title":"Produto teste","variation_id":12},"quantity":1,"unit_price":100.25}]}

def mock_api(monkeypatch,handler):
    real=httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:real(transport=httpx.MockTransport(handler),**kwargs))

def test_orders_no_connection():
    with TestClient(app) as c:
        assert c.post("/api/v1/integrations/mercadolivre/sync-orders").status_code==409
        assert c.get("/api/v1/marketplace-orders").json()["results"]==[]
        assert c.post("/api/v1/integrations/mercadolivre/sync-orders?days=0").status_code==422

def test_idempotent_import_status_update_and_no_stock_change(connected,monkeypatch):
    current=order();requests=[]
    def handler(r):
        requests.append(r);assert r.method=="GET";assert r.url.path=="/orders/search"
        assert r.url.params["seller"]=="123" and r.url.params["sort"]=="date_desc"
        assert r.headers["Authorization"]=="Bearer fake-access"
        return httpx.Response(200,json={"results":[current],"paging":{"total":1}})
    mock_api(monkeypatch,handler)
    with TestClient(app) as c:
        url="/api/v1/integrations/mercadolivre/sync-orders"
        assert c.post(url).status_code==200
        assert c.post(url).status_code==200
        payload=c.get("/api/v1/marketplace-orders").json();assert payload["total"]==1
        assert "PRIVATE" not in str(payload) and "buyer" not in str(payload)
        assert payload["results"][0]["total"]=="100.25"
        current=order(status="cancelled",updated="2026-11-05T13:00:00Z")
        assert c.post(url).status_code==200
        assert c.get("/api/v1/marketplace-orders?status=paid").json()["total"]==0
        assert c.get("/api/v1/marketplace-orders?status=cancelled&q=1").json()["total"]==1
        current=order(updated="2026-11-05T12:00:00Z")
        c.post(url)
        assert c.get("/api/v1/marketplace-orders").json()["results"][0]["status"]=="cancelled"
        assert c.get("/api/v1/marketplace-orders/summary").json()["paid_totals"]=={}
        report=c.get("/api/v1/reports/marketplace-orders.csv");assert report.status_code==200 and "cancelled" in report.text
    with SessionLocal() as db:
        assert db.query(Inventory).one().quantity==7 and db.query(Product).one().stock==10
        assert db.query(Order).count()==0 and db.query(ImportedOrder).count()==1

def test_pagination_and_atomic_failure(connected,monkeypatch):
    mode="success";offsets=[]
    def handler(r):
        n=int(r.url.params["offset"]);offsets.append(n)
        if mode=="failure" and n==1:return httpx.Response(503,json={"secret":"NEVER EXPOSE"})
        row=order(n+1,status="cancelled" if mode=="failure" else "paid")
        return httpx.Response(200,json={"results":[row],"paging":{"total":2}})
    mock_api(monkeypatch,handler)
    with TestClient(app) as c:
        url="/api/v1/integrations/mercadolivre/sync-orders"
        assert c.post(url).json()["imported"]==2 and offsets==[0,1]
        mode="failure"
        r=c.post(url);assert r.status_code==502 and "NEVER EXPOSE" not in r.text
        assert c.get("/api/v1/marketplace-orders?status=paid").json()["total"]==2
        runs=c.get("/api/v1/sync-runs").json();assert runs[0]["status"]=="failed"

def test_foreign_seller_rejected_and_permission_error(connected,monkeypatch):
    row=order();row["seller"]["id"]=999
    mock_api(monkeypatch,lambda r:httpx.Response(200,json={"results":[row],"paging":{"total":1}}))
    with TestClient(app) as c:
        assert c.post("/api/v1/integrations/mercadolivre/sync-orders").status_code==502
        assert c.get("/api/v1/marketplace-orders").json()["total"]==0
    # Replace the first mock factory rather than nesting transports.
    with SessionLocal() as db: assert db.query(ImportedOrder).count()==0

def test_expired_token_rotates_before_read(connected,monkeypatch):
    with SessionLocal() as db:
        conn=db.query(MarketplaceConnection).one();conn.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
    methods=[]
    def handler(r):
        methods.append((r.method,r.url.path))
        if r.url.path=="/oauth/token":
            data=parse_qs(r.content.decode());assert data["grant_type"]==["refresh_token"]
            assert data["refresh_token"]==["fake-refresh"]
            return httpx.Response(200,json={"user_id":123,"access_token":"new-fake-access","refresh_token":"new-fake-refresh","expires_in":21600})
        assert r.method=="GET" and r.headers["Authorization"]=="Bearer new-fake-access"
        return httpx.Response(200,json={"results":[],"paging":{"total":0}})
    mock_api(monkeypatch,handler)
    with TestClient(app) as c:
        assert c.post("/api/v1/integrations/mercadolivre/sync-orders").status_code==200
        assert c.post("/api/v1/integrations/mercadolivre/sync-orders").status_code==200
    assert methods==[("POST","/oauth/token"),("GET","/orders/search"),("GET","/orders/search")]
    with SessionLocal() as db:
        conn=db.query(MarketplaceConnection).one()
        assert decrypt(conn.refresh_token_encrypted)=="new-fake-refresh"
        assert conn.refresh_token_encrypted!="new-fake-refresh"

def test_read_denied_is_actionable(connected,monkeypatch):
    mock_api(monkeypatch,lambda r:httpx.Response(403,json={"message":"PRIVATE SERVER MESSAGE"}))
    with TestClient(app) as c:
        r=c.post("/api/v1/integrations/mercadolivre/sync-orders")
        assert r.status_code==502 and "permissões" in r.json()["detail"] and "PRIVATE" not in r.text


def test_partial_limit_and_account_isolation(connected,monkeypatch):
    def handler(r):
        offset=int(r.url.params["offset"])
        return httpx.Response(200,json={"results":[order(n+1) for n in range(offset,offset+50)],"paging":{"total":1050}})
    mock_api(monkeypatch,handler)
    with TestClient(app) as c:
        result=c.post("/api/v1/integrations/mercadolivre/sync-orders").json()
        assert result["partial"] is True and result["imported"]==1000 and result["total_found"]==1050
        assert c.get("/api/v1/marketplace-orders?offset=950&limit=100").json()["total"]==1000
        assert c.get("/api/v1/marketplace-orders/summary").json()["paid_totals"]=={"BRL":"100250.00"}
        with SessionLocal() as db:
            db.add(MarketplaceConnection(seller_id="999",access_token_encrypted=encrypt("other-access"),refresh_token_encrypted=encrypt("other-refresh"),expires_at=datetime.now(timezone.utc)+timedelta(hours=1)));db.commit()
        assert c.get("/api/v1/marketplace-orders").json()["total"]==0
        assert c.get("/api/v1/sync-runs").json()==[]
