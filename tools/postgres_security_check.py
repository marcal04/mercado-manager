"""Disposable PostgreSQL only. This script refuses the production database."""
import os
import secrets
from urllib.parse import quote
from sqlalchemy.engine import make_url

target = make_url(os.environ.get("DATABASE_URL", ""))
if target.host != "restore-check" or target.database != "restore_check":
    raise RuntimeError("Security checks require the isolated restore-check database.")
os.environ["AUTH_REQUIRED"] = "true"
os.environ["AUTO_SYNC_WORKER_ENABLED"] = "false"
os.environ["AI_ENABLED"] = "false"
from cryptography.fernet import Fernet
os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["APP_ACCESS_TOKEN"] = ""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from psycopg import sql
from app.main import app
from app.db import Base, engine, SessionLocal
from app.models import Product, MarketplaceConnection
from app.data_hub import upsert_snapshot
from app.marketplace_orders import ImportedOrder
from app.auth_guard import utcnow, AuthBudget
from app.security import encrypt
from datetime import timedelta

# Bootstrap only this disposable schema, then exercise application routes using
# a login with CRUD rights and no schema/server administration permissions.
Base.metadata.create_all(engine)
role, role_password = "isolated_security_app", secrets.token_urlsafe(32)
with engine.begin() as connection:
    connection.exec_driver_sql(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS").format(sql.Identifier(role), sql.Literal(role_password)).as_string())
    connection.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    connection.exec_driver_sql("GRANT USAGE ON SCHEMA public TO isolated_security_app")
    connection.exec_driver_sql("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO isolated_security_app")
    connection.exec_driver_sql("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO isolated_security_app")
restricted = create_engine(target.set(username=role, password=role_password))
SessionLocal.configure(bind=restricted)

with restricted.connect() as connection:
    privileges = connection.execute(text("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
    assert not any(privileges)
try:
    with restricted.begin() as connection:
        connection.execute(text("CREATE TABLE forbidden_security_probe(id integer)"))
    raise AssertionError("Restricted role unexpectedly created a table")
except DBAPIError as error:
    assert getattr(error.orig, "sqlstate", None) == "42501"

password = "Isolated!"+secrets.token_hex(16)
payloads = ["' OR '1'='1' --", "' UNION SELECT password_hash FROM operator_account --", "'; DROP TABLE products; --"]
with TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)) as client:
    assert client.post("/session/setup", json={"password": password, "confirmation": password}).status_code == 200
    assert client.post("/api/v1/products", json={"sku":"isolated-item", "name":"Isolated product", "brand":"Test", "reference_price":100, "stock":3}).status_code == 201
    with SessionLocal() as db:
        db.add(MarketplaceConnection(seller_id="123", access_token_encrypted=encrypt("fake"), refresh_token_encrypted=encrypt("fake"), expires_at=utcnow()+timedelta(hours=1)))
        upsert_snapshot(db,"123","account","owned-record","/users/me",{"id":123})
        db.add(ImportedOrder(seller_id="123",external_id="owned-order",status="paid",currency="BRL",total=100,created_at=utcnow(),updated_at=utcnow(),imported_at=utcnow(),items=[]))
        db.commit()
    for payload in payloads:
        records = client.get("/api/v1/data-hub/records", params={"module":"account", "q":payload})
        orders = client.get("/api/v1/marketplace-orders", params={"q":payload})
        assert records.status_code == orders.status_code == 200
        assert records.json()["total"] == orders.json()["total"] == 0
    assert client.get("/api/v1/data-hub/records/"+quote("1 OR 1=1",safe="")).status_code == 422
    assert client.delete("/session").status_code == 200
    for payload in payloads:
        response = client.post("/session", json={"password":payload})
        assert response.status_code == 401 and payload not in response.text
    for payload in ("admin", "' OR 1=1 --", "a"*43):
        client.cookies.set("manager_session", payload)
        assert client.get("/api/v1/catalog").status_code == 401
    client.cookies.clear()
    assert client.post("/session",json={"password":password}).status_code == 200
    assert len(client.get("/api/v1/catalog").json()) == 1
    with SessionLocal() as db:
        assert db.query(Product).count() == 1
    assert client.post("/session/setup",json={"password":"Overwrite!123456", "confirmation":"Overwrite!123456", "role":"admin"}).status_code == 409
    print("Isolated PostgreSQL checks passed: SQLi login/filter/cookie probes, access control and restricted database role.")

with TestClient(app,base_url="http://localhost:8000",client=("192.0.2.1",50000)) as first:
    for _ in range(5):
        assert first.post("/session",json={"password":"incorrect"}).status_code == 401
with TestClient(app,base_url="http://localhost:8000",client=("198.51.100.1",50000)) as second:
    assert second.post("/session",json={"password":password}).status_code == 429
    print("Persistent login budget survived a new client, address and application lifespan.")
restricted.dispose()
engine.dispose()
