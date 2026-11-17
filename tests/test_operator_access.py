from datetime import datetime,timedelta,timezone
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import get_settings
from app.db import SessionLocal
from app.access import Operator,OperatorSession,token_hash

PASSWORD="Local test password 123!"

@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED","true")
    get_settings.cache_clear()

def client():return TestClient(app,base_url="http://localhost:8000")

def setup(c,password=PASSWORD):return c.post("/session/setup",json={"password":password,"confirmation":password})

def test_first_access_is_closed_until_password_is_created(secured):
    with client() as c:
        assert c.get("/",follow_redirects=False).status_code==303
        assert c.get("/api/v1/catalog").status_code==401
        assert c.get("/static/manager.js").status_code==401
        page=c.get("/login")
        assert "Crie seu acesso" in page.text and PASSWORD not in page.text
        assert c.get("/health").status_code==200
        assert setup(c).status_code==200
        assert c.get("/api/v1/catalog").status_code==200
        assert c.post("/session/setup",json={"password":PASSWORD,"confirmation":PASSWORD}).status_code==409
    with SessionLocal() as db:
        stored=db.get(Operator,1).password_hash
        assert PASSWORD not in stored and stored.startswith("scrypt-")
        session=db.query(OperatorSession).first()
        assert len(session.token_hash)==64

def test_logout_revokes_session_and_cookie_replay(secured):
    with client() as c:
        assert setup(c).status_code==200
        token=c.cookies.get("manager_session")
        assert c.delete("/session").status_code==200
        c.cookies.set("manager_session",token)
        assert c.get("/api/v1/catalog").status_code==401
        assert c.post("/session",json={"password":PASSWORD}).status_code==200
        assert c.get("/api/v1/catalog").status_code==200
        assert "Bem-vindo de volta" in c.get("/login").text

def test_session_tampering_and_expiry(secured):
    with client() as c:
        assert setup(c).status_code==200
        token=c.cookies.get("manager_session")
        c.cookies.set("manager_session",token[:-1]+('a' if token[-1]!='a' else 'b'),domain='localhost.local',path='/')
        assert c.get("/api/v1/catalog").status_code==401
        c.cookies.clear();c.cookies.set("manager_session",token)
        with SessionLocal() as db:
            db.get(OperatorSession,token_hash(token)).expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
        assert c.get("/api/v1/catalog").status_code==401

def test_setup_rejects_weak_mismatched_and_remote_password_entries(secured,monkeypatch):
    with client() as c:
        assert setup(c,"short").status_code==422
        response=c.post("/session/setup",json={"password":PASSWORD,"confirmation":"different"})
        assert response.status_code==422 and PASSWORD not in response.text
        assert c.post("/session/setup",json={"password":PASSWORD,"confirmation":PASSWORD},headers={"origin":"https://evil.example"}).status_code==403
    monkeypatch.setenv("ALLOWED_HOSTS","localhost,remote.example");get_settings.cache_clear()
    with TestClient(app,base_url="https://remote.example") as c:
        assert setup(c).status_code==403
    with SessionLocal() as db:assert db.get(Operator,1) is None

def test_login_limit_and_invalid_bodies_do_not_echo_secrets(secured):
    with client() as c:
        assert setup(c).status_code==200
        assert c.delete("/session").status_code==200
        for _ in range(5):
            response=c.post("/session",json={"password":"wrong secret"})
            assert response.status_code==401 and "wrong secret" not in response.text
        assert c.post("/session",json={"password":PASSWORD}).status_code==429

def test_host_csrf_and_security_headers(secured):
    with client() as c:
        assert setup(c).status_code==200
        assert c.get("/api/v1/catalog",headers={"host":"evil.example"}).status_code==400
        assert c.post("/api/v1/products",json={},headers={"sec-fetch-site":"cross-site"}).status_code==403
        assert c.post("/api/v1/products",json={},headers={"origin":"https://localhost:8000"}).status_code==403
        response=c.get("/")
        assert response.headers["x-content-type-options"]=="nosniff"
        assert response.headers["x-frame-options"]=="DENY"
        assert response.headers["cache-control"]=="no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert c.get("/docs").status_code==404
        assert c.get("/openapi.json").status_code==404

def test_cookie_is_httponly_and_password_never_returned(secured):
    with client() as c:
        response=setup(c)
        assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
        assert PASSWORD not in response.text
        token=c.cookies.get("manager_session")
        with SessionLocal() as db:
            assert token not in db.query(OperatorSession).first().token_hash

def test_authorized_operator_can_read_private_snapshots(secured,monkeypatch):
    from cryptography.fernet import Fernet
    from app.models import MarketplaceConnection
    from app.security import encrypt
    from app.data_hub import upsert_snapshot
    monkeypatch.setenv("APP_ENCRYPTION_KEY",Fernet.generate_key().decode());get_settings.cache_clear()
    with SessionLocal() as db:
        db.add(MarketplaceConnection(seller_id="123",access_token_encrypted=encrypt("fake"),refresh_token_encrypted=encrypt("fake"),expires_at=datetime.now(timezone.utc)+timedelta(hours=1)))
        upsert_snapshot(db,"123","account","123","/users/me",{"id":123});db.commit()
    with client() as c:
        assert c.get("/api/v1/data-hub/records/1").status_code==401
        assert setup(c).status_code==200
        assert c.get("/api/v1/data-hub/records/1").json()["data"]["id"]==123
