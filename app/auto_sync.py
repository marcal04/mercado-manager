"""Durable read-only synchronization; notifications are hints, never trusted data."""
import asyncio
import hashlib
import json
import logging
import re
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .config import get_settings
from .data_hub import DataJob, execute_job, job_out
from .db import Base, SessionLocal, get_db
from .marketplace_orders import latest
from .models import AuditEvent, now

logger = logging.getLogger(__name__)
router = APIRouter()
MIN_EVENT_GAP = timedelta(minutes=15)
TOPICS = {
    "items": r"/items/[A-Z]{3}\d+",
    "orders_v2": r"/orders/\d+",
    "shipments": r"/shipments/\d+",
    "questions": r"/questions/\d+",
    "claims": r"/(?:post-purchase/v1/)?claims/\d+",
    "messages": r"(?:/messages/)?[A-Za-z0-9_-]+",
    "user-products": r"/user-products/[A-Za-z0-9_-]+",
}


class AutoSync(Base):
    __tablename__ = "auto_sync_controls"
    seller_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=360)
    next_run_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)


class Notification(Base):
    __tablename__ = "marketplace_notifications"
    __table_args__ = (UniqueConstraint("seller_id", "fingerprint"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[str] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(40))
    received_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def control(db, seller, stamp=None):
    row = db.get(AutoSync, seller)
    if not row:
        row = AutoSync(seller_id=seller, next_run_at=stamp or now())
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.get(AutoSync, seller)
    return row


def reconcile(db, row, stamp):
    if not row.active_job_id:
        return
    job = db.get(DataJob, row.active_job_id)
    if job and job.status == "running":
        return
    success = job and job.status in ("completed", "completed_with_gaps")
    db.query(Notification).filter_by(seller_id=row.seller_id, job_id=row.active_job_id, status="queued").update(
        {Notification.status: "checked" if success else "pending", Notification.job_id: row.active_job_id if success else None})
    row.failures = 0 if success else row.failures + 1
    delay = row.interval_minutes if success else min(360, 15 * 2 ** min(row.failures - 1, 5))
    row.next_run_at = stamp + timedelta(minutes=delay)
    row.active_job_id = None
    db.commit()


def reserve_job(db, stamp=None):
    stamp = stamp or now()
    connection = latest(db)
    if not connection:
        return None
    row = control(db, connection.seller_id, stamp)
    reconcile(db, row, stamp)
    if not row.enabled or row.active_job_id:
        return None
    if db.query(DataJob).filter_by(seller_id=row.seller_id, status="running").first():
        return None
    last = db.query(DataJob).filter_by(seller_id=row.seller_id).order_by(DataJob.id.desc()).first()
    last_start = max((utc(x) for x in (row.last_started_at, last.started_at if last else None) if x), default=None)
    pending = db.query(Notification).filter_by(seller_id=row.seller_id, status="pending").first()
    # A failed run's next_run_at is also its retry barrier; incoming hints cannot bypass it.
    due = stamp >= utc(row.next_run_at)
    event_due = pending and row.failures == 0 and (not last_start or stamp - last_start >= MIN_EVENT_GAP)
    if not due and not event_due:
        return None
    if last_start and stamp - last_start < MIN_EVENT_GAP:
        return None
    job = DataJob(seller_id=row.seller_id, status="running", progress={})
    db.add(job)
    try:
        db.flush()  # Same unique running-job constraint as manual synchronization.
        row.active_job_id = job.id
        row.last_started_at = stamp
        db.query(Notification).filter_by(seller_id=row.seller_id, status="pending").update(
            {Notification.status: "queued", Notification.job_id: job.id})
        db.add(AuditEvent(event_type="data_hub.auto_started", entity_type="data_job", entity_id=str(job.id),
                          detail={"trigger": "notification_hint" if pending else "schedule"}))
        db.commit()
        return job.id
    except IntegrityError:
        db.rollback()
        return None


async def tick():
    with SessionLocal() as db:
        job_id = reserve_job(db)
    if job_id:
        await execute_job(job_id)
        with SessionLocal() as db:
            job = db.get(DataJob, job_id)
            reconcile(db, db.get(AutoSync, job.seller_id), now())


async def worker():
    # Delay startup so the API is ready before the first provider request.
    while True:
        await asyncio.sleep(10)
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not log credentials or webhook payloads.
            logger.error("Automatic synchronization tick failed; will retry.")


class Preferences(BaseModel):
    enabled: bool
    interval_minutes: int = Field(ge=60, le=1440)


@router.get("/api/v1/auto-sync")
def status(db: Session = Depends(get_db)):
    connection = latest(db)
    if not connection:
        return {"connected": False, "enabled": False, "history": [], "notifications": {}}
    row = control(db, connection.seller_id)
    counts = dict(db.query(Notification.status, func.count(Notification.id)).filter_by(seller_id=row.seller_id).group_by(Notification.status).all())
    recent = db.query(DataJob).filter_by(seller_id=row.seller_id).order_by(DataJob.id.desc()).limit(10).all()
    last_start = max((utc(x) for x in (row.last_started_at, recent[0].started_at if recent else None) if x), default=None)
    next_run = max(utc(row.next_run_at), last_start + MIN_EVENT_GAP) if last_start else row.next_run_at
    automatic_ids = {e.entity_id for e in db.query(AuditEvent).filter_by(event_type="data_hub.auto_started").filter(AuditEvent.entity_id.in_([str(j.id) for j in recent])).all()}
    return {"connected": True, "enabled": row.enabled, "interval_minutes": row.interval_minutes,
            "next_run_at": next_run, "last_started_at": row.last_started_at, "failures": row.failures,
            "active_job_id": row.active_job_id, "notifications": counts,
            "webhook_path": "/api/v1/webhooks/mercadolivre", "notification_received": bool(counts),
            "history": [{k: v for k, v in job_out(j).items() if k != "progress"} | {
                "trigger": "automatic" if str(j.id) in automatic_ids else "manual",
                "issues": sum(1 for v in j.progress.values() if isinstance(v, dict) and v.get("status") in ("failed", "partial", "denied", "unavailable", "blocked"))} for j in recent]}


@router.put("/api/v1/auto-sync")
def preferences(body: Preferences, db: Session = Depends(get_db)):
    connection = latest(db)
    if not connection:
        raise HTTPException(409, "Conecte sua conta em Ajustes.")
    row = control(db, connection.seller_id)
    row.enabled = body.enabled
    row.interval_minutes = body.interval_minutes
    row.next_run_at = now() + timedelta(minutes=body.interval_minutes)
    db.add(AuditEvent(event_type="data_hub.auto_preferences", entity_type="integration", entity_id=row.seller_id, detail=body.model_dump()))
    db.commit()
    return status(db)


@router.post("/api/v1/webhooks/mercadolivre")
async def receive(request: Request, db: Session = Depends(get_db)):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 16384:
            raise HTTPException(413, "Notificação excede o limite.")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Notificação inválida.")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Notificação inválida.")
    connection = latest(db)
    app_id = get_settings().ml_client_id
    topic = payload.get("topic")
    resource = payload.get("resource")
    if (not connection or not app_id or str(payload.get("application_id")) != app_id
            or str(payload.get("user_id")) != connection.seller_id
            or not isinstance(topic, str) or topic not in TOPICS
            or not isinstance(resource, str) or len(resource) > 300
            or not re.fullmatch(TOPICS[topic], resource)):
        return {"accepted": False, "reason": "unsupported_or_unlinked"}
    # No payload values update operational data or become URLs. A bounded queue only
    # requests a complete authorized account refresh through the existing GET client.
    identity = payload.get("_id") or payload.get("sent")
    if not isinstance(identity, (str, int)) or len(str(identity)) > 200:
        raise HTTPException(400, "Identificador ou data ausente.")
    fingerprint = hashlib.sha256(json.dumps([str(identity), topic, resource, app_id], separators=(",", ":")).encode()).hexdigest()
    if db.query(Notification).filter_by(seller_id=connection.seller_id, fingerprint=fingerprint).first():
        return {"accepted": True, "duplicate": True}
    if db.query(Notification).filter(Notification.seller_id == connection.seller_id, Notification.status.in_(["pending", "queued"])).count() >= 10000:
        raise HTTPException(429, "Fila temporariamente cheia.")
    db.add(Notification(seller_id=connection.seller_id, topic=topic, fingerprint=fingerprint))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return {"accepted": True, "queued": True}
