from datetime import timedelta
import asyncio
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import MarketplaceConnection, now
from app.config import get_settings
from app.auto_sync import AutoSync, Notification, control, reserve_job, reconcile, tick
from app.data_hub import DataJob

@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv("ML_CLIENT_ID", "456")
    get_settings.cache_clear()
    with SessionLocal() as db:
        db.add(MarketplaceConnection(seller_id="123", access_token_encrypted="unused", refresh_token_encrypted="unused", expires_at=now()+timedelta(hours=1)))
        db.commit()

def event(**overrides):
    return {"_id":"event-1", "application_id":456, "user_id":123, "topic":"items", "resource":"/items/MLB123", "sent":"2026-11-05T12:00:00Z", **overrides}

def test_webhook_is_durable_deduplicated_hint_only(connected):
    with TestClient(app) as c:
        assert c.post("/api/v1/webhooks/mercadolivre",json=event()).json()["queued"]
        assert c.post("/api/v1/webhooks/mercadolivre",json=event()).json()["duplicate"]
        assert c.get("/api/v1/auto-sync").json()["notifications"]=={"pending":1}
    with SessionLocal() as db:
        assert db.query(Notification).count()==1
        assert db.query(DataJob).count()==0
        n=db.query(Notification).first()
        assert "MLB123" not in str(n.__dict__)  # No arbitrary resource or payload retained.

@pytest.mark.parametrize("overrides",[
    {"user_id":999},{"application_id":999},{"topic":"unknown"},
    {"resource":"https://attacker.example/items/MLB1"},
    {"resource":"/items/MLB1?token=secret"},{"resource":"/items/../users/1"},
    {"topic":[]},{"resource":{}},
])
def test_rejects_foreign_and_unsafe_hints(connected,overrides):
    with TestClient(app) as c:
        assert c.post("/api/v1/webhooks/mercadolivre",json=event(**overrides)).json()["accepted"] is False
    with SessionLocal() as db:assert db.query(Notification).count()==0

def test_webhook_size_and_shape_limits(connected):
    with TestClient(app) as c:
        assert c.post("/api/v1/webhooks/mercadolivre",content="x"*17000).status_code==413
        assert c.post("/api/v1/webhooks/mercadolivre",json=[]).status_code==400
        assert c.post("/api/v1/webhooks/mercadolivre",content="{").status_code==400
        assert c.post("/api/v1/webhooks/mercadolivre",json=event(_id=None,sent=None)).status_code==400

def test_schedule_reservation_does_not_overlap_manual_job(connected):
    stamp=now()
    with SessionLocal() as db:
        row=control(db,"123");row.next_run_at=stamp-timedelta(seconds=1)
        manual=DataJob(seller_id="123",status="running",progress={});db.add(manual);db.commit()
        assert reserve_job(db,stamp) is None
        manual.status="completed";db.commit()
        assert reserve_job(db,stamp+timedelta(minutes=1)) is None
        job_id=reserve_job(db,stamp+timedelta(minutes=16))
        assert job_id
        assert reserve_job(db,stamp+timedelta(hours=1)) is None

def test_event_during_run_is_not_lost_and_burst_is_coalesced(connected):
    stamp=now()
    with SessionLocal() as db:
        row=control(db,"123");row.next_run_at=stamp-timedelta(seconds=1);db.commit()
        first=Notification(seller_id="123",fingerprint="first",topic="items")
        db.add(first);db.commit()
        job_id=reserve_job(db,stamp);db.refresh(first)
        assert first.status=="queued" and first.job_id==job_id
        db.add(Notification(seller_id="123",fingerprint="during",topic="orders_v2"));db.commit()
        job=db.get(DataJob,job_id);job.status="completed_with_gaps";db.commit()
        reconcile(db,row,stamp+timedelta(minutes=1));db.refresh(first)
        assert first.status=="checked"
        assert reserve_job(db,stamp+timedelta(minutes=2)) is None
        next_id=reserve_job(db,stamp+timedelta(minutes=16))
        assert next_id and next_id!=job_id
        assert db.query(Notification).filter_by(status="queued").count()==1

def test_failure_backoff_survives_restart_and_blocks_event_retry(connected):
    stamp=now()
    with SessionLocal() as db:
        row=control(db,"123");row.next_run_at=stamp-timedelta(seconds=1)
        db.add(Notification(seller_id="123",fingerprint="failed",topic="items"));db.commit()
        job_id=reserve_job(db,stamp)
        job=db.get(DataJob,job_id);job.status="interrupted";db.commit()
    with SessionLocal() as db:
        row=db.get(AutoSync,"123");reconcile(db,row,stamp+timedelta(minutes=1))
        assert row.failures==1
        assert db.query(Notification).first().status=="pending"
        assert reserve_job(db,stamp+timedelta(minutes=15)) is None
        retry=reserve_job(db,stamp+timedelta(minutes=17));assert retry
        db.get(DataJob,retry).status="failed";db.commit()
        reconcile(db,row,stamp+timedelta(minutes=18))
        assert row.failures==2
        assert reserve_job(db,stamp+timedelta(minutes=47)) is None

def test_pause_preferences_and_account_isolation(connected):
    with TestClient(app) as c:
        assert c.put("/api/v1/auto-sync",json={"enabled":False,"interval_minutes":60}).status_code==200
        assert not c.get("/api/v1/auto-sync").json()["enabled"]
        assert c.put("/api/v1/auto-sync",json={"enabled":True,"interval_minutes":0}).status_code==422
    with SessionLocal() as db:
        row=db.get(AutoSync,"123");row.next_run_at=now()-timedelta(days=1);db.commit()
        db.add(DataJob(seller_id="999",status="failed",progress={}));db.commit()
        assert reserve_job(db) is None
    with TestClient(app) as c:assert not c.get("/api/v1/auto-sync").json()["history"]

def test_worker_executes_and_reconciles_with_independent_sessions(connected,monkeypatch):
    invoked=[]
    async def execute(job_id):
        invoked.append(job_id)
        with SessionLocal() as db:
            db.get(DataJob,job_id).status="completed";db.commit()
    monkeypatch.setattr("app.auto_sync.execute_job",execute)
    asyncio.run(tick())
    assert len(invoked)==1
    with SessionLocal() as db:
        row=db.get(AutoSync,"123")
        assert row.active_job_id is None and row.failures==0
        assert reserve_job(db) is None
    with TestClient(app) as c:
        assert c.get("/api/v1/auto-sync").json()["history"][0]["trigger"]=="automatic"

def test_no_account_and_authenticated_settings(monkeypatch):
    with TestClient(app) as c:
        assert not c.get("/api/v1/auto-sync").json()["connected"]
        assert c.put("/api/v1/auto-sync",json={"enabled":True,"interval_minutes":60}).status_code==409
    monkeypatch.setenv("APP_ACCESS_TOKEN","test-password");get_settings.cache_clear()
    with TestClient(app) as c:
        assert c.get("/api/v1/auto-sync").status_code==401
        assert c.put("/api/v1/auto-sync",json={"enabled":False,"interval_minutes":60}).status_code==401
        # Provider callback remains public, but cannot create an unrelated queue entry.
        assert c.post("/api/v1/webhooks/mercadolivre",json=event()).status_code==200
