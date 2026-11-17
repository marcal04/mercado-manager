"""Persistent, atomic attempt budgets and a single active operator session."""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Integer, String, case, or_
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal
from .origins import secure_request

ATTEMPT_COOKIE = "manager_attempt"
SIGNING_KEY = secrets.token_bytes(32)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class AuthBudget(Base):
    __tablename__ = "authentication_budgets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SessionState(Base):
    __tablename__ = "operator_session_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    agent_hash: Mapped[str] = mapped_column(String(64))
    idle_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def utcnow():
    return datetime.now(timezone.utc)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def insert_for(db, model):
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model)


def browser_cookie(value):
    try:
        token, signature = value.split(".")
        return bool(TOKEN_PATTERN.fullmatch(token) and hmac.compare_digest(
            signature, hmac.new(SIGNING_KEY, token.encode(), hashlib.sha256).hexdigest()))
    except (ValueError, AttributeError, TypeError):
        return False


def issue_browser_cookie(response, request):
    if not browser_cookie(request.cookies.get(ATTEMPT_COOKIE)):
        token = secrets.token_urlsafe(32)
        signature = hmac.new(SIGNING_KEY, token.encode(), hashlib.sha256).hexdigest()
        response.set_cookie(ATTEMPT_COOKIE, token+"."+signature, httponly=True, samesite="strict",
                            max_age=900, secure=secure_request(request))
    return response


def keys(request, realm):
    # Forwarded headers are not used to select a budget. The account budget also
    # prevents bypass by rotating addresses, clients, or pre-login cookies.
    peer = request.client.host if request.client else "unknown"
    result = [sha(realm+":peer:"+peer)]
    if realm == "login":
        result.insert(0, sha("login:single-operator"))
        value = request.cookies.get(ATTEMPT_COOKIE)
        if browser_cookie(value):
            result.append(sha("login:browser:"+value))
    return result


def limited(request, realm="login"):
    stamp = utcnow()
    limit, seconds = (5, 300) if realm == "login" else (30, 60)
    with SessionLocal() as db:
        db.query(AuthBudget).filter(AuthBudget.until <= stamp).delete()
        for identity in keys(request, realm):
            expired = AuthBudget.until <= stamp
            statement = insert_for(db, AuthBudget).values(key=identity, attempts=1, until=stamp+timedelta(seconds=seconds))
            statement = statement.on_conflict_do_update(index_elements=["key"], set_={
                "attempts": case((expired, 1), else_=AuthBudget.attempts+1),
                "until": case((expired, stamp+timedelta(seconds=seconds)), else_=AuthBudget.until)},
                where=or_(expired, AuthBudget.attempts < limit)).returning(AuthBudget.attempts)
            if db.execute(statement).scalar_one_or_none() is None:
                db.rollback()
                return True
        db.commit()
    return False


def clear_limit(request):
    with SessionLocal() as db:
        db.query(AuthBudget).filter(AuthBudget.key.in_(keys(request, "login"))).delete()
        db.commit()


def activate(db, token_hash, request):
    statement = insert_for(db, SessionState).values(id=1, token_hash=token_hash,
        agent_hash=sha(request.headers.get("user-agent", "")), idle_until=utcnow()+timedelta(minutes=30))
    db.execute(statement.on_conflict_do_update(index_elements=["id"], set_={
        k: statement.excluded[k] for k in ("token_hash", "agent_hash", "idle_until")}))


def active(db, token_hash, agent):
    stamp = utcnow()
    query = db.query(SessionState).filter(SessionState.id == 1, SessionState.token_hash == token_hash,
                SessionState.agent_hash == sha(agent), SessionState.idle_until > stamp)
    updated = query.update({SessionState.idle_until: stamp+timedelta(minutes=30)})
    db.commit()
    return bool(updated)
