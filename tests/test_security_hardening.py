import base64
from datetime import timedelta
from urllib.parse import quote
import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from app.main import app
from app.config import get_settings
from app.db import SessionLocal
from app.access import Operator,OperatorSession,hash_password,matches
from app.auth_guard import AuthBudget,SessionState,utcnow
from app.models import Product,MarketplaceConnection,AuditEvent
from app.marketplace_orders import ImportedOrder
from app.data_hub import upsert_snapshot
from app.security import encrypt

PASSWORD="Local test password 123!"
PAYLOADS=["' OR '1'='1' --", "' UNION SELECT password_hash FROM operator_account --", "'; DROP TABLE products; --", "1 OR 1=1", "' AND (SELECT pg_sleep(5)) IS NULL --"]

@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED","true")
    monkeypatch.setenv("APP_ENCRYPTION_KEY",Fernet.generate_key().decode())
    get_settings.cache_clear()

def client(peer="127.0.0.1"):
    return TestClient(app,base_url="http://localhost:8000",client=(peer,50000))

def setup(c,password=PASSWORD):return c.post("/session/setup",json={"password":password,"confirmation":password})

def seed(c):
    assert setup(c).status_code==200
    assert c.post("/api/v1/products",json={"sku":"safe-product","name":"Protected product","brand":"Test","reference_price":100,"stock":3}).status_code==201
    with SessionLocal() as db:
        db.add(MarketplaceConnection(seller_id="123",access_token_encrypted=encrypt("fake"),refresh_token_encrypted=encrypt("fake"),expires_at=utcnow()+timedelta(hours=1)))
        upsert_snapshot(db,"123","account","own-record","/users/me",{"id":123})
        db.add(ImportedOrder(seller_id="123",external_id="own-order",status="paid",currency="BRL",total=100,created_at=utcnow(),updated_at=utcnow(),imported_at=utcnow(),items=[]))
        db.commit()

def test_scrypt_unique_salt_and_verification():
    a=hash_password(PASSWORD);b=hash_password(PASSWORD)
    assert a!=b and matches(PASSWORD,a) and matches(PASSWORD,b) and not matches("wrong",a)
    assert len(base64.urlsafe_b64decode(a.split("$")[1]))==16
    assert PASSWORD not in a

@pytest.mark.parametrize("password",["onlyletterslong", "12345678901234!", "OnlyLetters123456", "LettersAndSymbols!", "Letters12345   "])
def test_complexity_is_enforced_on_server(secured,password):
    with client() as c:
        response=setup(c,password)
        assert response.status_code==422 and password not in response.text
    with SessionLocal() as db:assert db.get(Operator,1) is None

def test_account_budget_survives_cookie_ip_and_client_rotation(secured):
    with client() as c:assert setup(c).status_code==200
    for n in range(5):
        with client("192.0.2."+str(n+1)) as c:
            c.get("/login")
            assert c.post("/session",json={"password":"incorrect"}).status_code==401
    with client("198.51.100.1") as c:
        c.cookies.clear()
        assert c.post("/session",json={"password":PASSWORD}).status_code==429
    with SessionLocal() as db:
        assert db.query(AuthBudget).count()>0
        db.query(AuthBudget).update({AuthBudget.until:utcnow()-timedelta(seconds=1)});db.commit()
    with client() as c:assert c.post("/session",json={"password":PASSWORD}).status_code==200

def test_browser_cookie_and_ip_budgets_are_additional_layers(secured):
    from app.auth_guard import keys,sha
    with client() as c:
        c.get("/login")
        assert c.cookies.get("manager_attempt")
        assert c.post("/session",json={"password":"wrong"}).status_code==401
        with SessionLocal() as db:
            assert db.query(AuthBudget).count()==3
            # Removing only the account counter cannot remove the source and browser counters.
            db.query(AuthBudget).filter_by(key=sha("login:single-operator")).delete();db.commit()
            assert db.query(AuthBudget).count()==2

def test_idle_timeout_single_active_session_and_agent_binding(secured):
    with client() as first:
        assert setup(first).status_code==200
        with client() as second:
            assert second.post("/session",json={"password":PASSWORD}).status_code==200
            assert first.get("/api/v1/catalog").status_code==401
            assert second.get("/api/v1/catalog",headers={"user-agent":"another-client"}).status_code==401
            assert second.get("/api/v1/catalog").status_code==200
            with SessionLocal() as db:
                db.query(SessionState).update({SessionState.idle_until:utcnow()-timedelta(seconds=1)});db.commit()
            assert second.get("/api/v1/catalog").status_code==401

def test_anonymous_cookie_guessing_is_limited(secured):
    with client() as c:
        for n in range(30):
            c.cookies.set("manager_session","' OR 1=1 --"+str(n))
            assert c.get("/api/v1/catalog").status_code==401
        assert c.get("/api/v1/catalog").status_code==429

def test_sql_injection_cannot_authenticate_or_destroy_data(secured):
    with client() as c:
        seed(c)
        assert c.delete("/session").status_code==200
        for payload in PAYLOADS:
            r=c.post("/session",json={"password":payload})
            assert r.status_code==401 and payload not in r.text
        assert c.get("/api/v1/catalog").status_code==401
    with SessionLocal() as db:
        assert db.query(Product).count()==1 and db.get(Operator,1)
        assert db.query(OperatorSession).count()==0
        for event in db.query(AuditEvent).filter_by(event_type="security.login_failed"):
            assert event.detail=={}

def test_sql_injection_filters_remain_literal_and_account_scoped(secured):
    with client() as c:
        seed(c)
        for payload in PAYLOADS+["%", "_"]:
            orders=c.get("/api/v1/marketplace-orders",params={"q":payload})
            records=c.get("/api/v1/data-hub/records",params={"module":"account","q":payload})
            assert orders.status_code==records.status_code==200
            assert orders.json()["total"]==records.json()["total"]==0
            assert c.get("/api/v1/marketplace-orders",params={"status":payload}).json()["total"]==0
        assert c.get("/api/v1/marketplace-orders",params={"q":"own-order"}).json()["total"]==1
        assert c.get("/api/v1/data-hub/records",params={"module":"account"}).json()["total"]==1
        assert c.get("/api/v1/data-hub/records",params={"module":"account' OR 1=1 --"}).status_code==422
        assert c.get("/api/v1/data-hub/records/"+quote("1 OR 1=1",safe='')).status_code==422
    with SessionLocal() as db:assert db.query(Product).count()==1

def test_privilege_claims_and_forged_cookies_do_not_grant_access(secured):
    with client() as c:
        seed(c)
        assert c.delete("/session").status_code==200
        for value in ["admin", "1", "' OR 1=1 --", "a"*43]:
            c.cookies.set("manager_session",value)
            r=c.get("/api/v1/catalog")
            assert r.status_code==401 and "Protected product" not in r.text
        c.cookies.clear()
        r=c.post("/session",json={"password":"wrong","role":"admin","is_admin":True,"operator_id":1})
        assert r.status_code==401
        r=c.post("/session/setup",json={"password":"ReplacementPass123!","confirmation":"ReplacementPass123!","id":1,"role":"admin"})
        assert r.status_code==409
        assert c.post("/session",json={"password":PASSWORD}).status_code==200
        assert c.get("/api/v1/catalog").json()[0]["name"]=="Protected product"

def test_https_cookie_flags_and_login_has_no_hash_or_token(secured):
    with TestClient(app,base_url="https://localhost:8000") as c:
        response=setup(c)
        header=response.headers["set-cookie"]
        assert "Secure" in header and "HttpOnly" in header and "SameSite=strict" in header
        page=c.get("/login")
        with SessionLocal() as db:encoded=db.get(Operator,1).password_hash
        assert encoded not in page.text and PASSWORD not in page.text and c.cookies.get("manager_session") not in page.text
        assert page.headers["x-frame-options"]=="DENY" and "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_parallel_login_attempts_cannot_exceed_account_budget(secured):
    from concurrent.futures import ThreadPoolExecutor
    with client() as c:assert setup(c).status_code==200
    def attempt(n):
        # Independent clients, cookies and addresses still share one account budget.
        with client("192.0.2."+str(n+10)) as c:
            return c.post("/session",json={"password":"bad-password"}).status_code
    with ThreadPoolExecutor(max_workers=6) as pool:statuses=list(pool.map(attempt,range(10)))
    assert statuses.count(401)==5 and statuses.count(429)==5


def test_password_change_requires_current_password_and_rotates_cookie(secured):
    new="New password 456!789"
    with client() as c:
        assert setup(c).status_code==200
        before=c.cookies.get("manager_session")
        weak={"current_password":PASSWORD,"new_password":"OnlyLetters123456","confirmation":"OnlyLetters123456"}
        assert c.post("/session/password",json=weak).status_code==422
        body={"current_password":"wrong","new_password":new,"confirmation":new}
        r=c.post("/session/password",json=body)
        assert r.status_code==401 and "wrong" not in r.text
        body["current_password"]=PASSWORD
        assert c.post("/session/password",json=body).status_code==200
        assert c.cookies.get("manager_session")!=before
        with client() as attacker:
            attacker.cookies.set("manager_session",before)
            assert attacker.get("/api/v1/catalog").status_code==401
            assert attacker.post("/session/password",json=body).status_code==401
        assert c.delete("/session").status_code==200
        assert c.post("/session",json={"password":PASSWORD}).status_code==401
        assert c.post("/session",json={"password":new}).status_code==200
