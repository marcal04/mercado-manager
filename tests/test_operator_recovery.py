from datetime import timedelta
import pytest
from app.access import Operator,OperatorSession,hash_password,matches
from app.auth_guard import SessionState,AuthBudget,utcnow
from app.models import Product,AuditEvent
from app.db import SessionLocal
from app.operator_recovery import reset_operator,main


def test_local_recovery_replaces_hash_revokes_sessions_and_preserves_products():
    old="PreviousSecret123!";new="FreshOperatorPass456!"
    with SessionLocal() as db:
        db.add(Operator(id=1,password_hash=hash_password(old)))
        db.add(OperatorSession(token_hash="a"*64,expires_at=utcnow()+timedelta(hours=1)))
        db.add(SessionState(id=1,token_hash="a"*64,agent_hash="b"*64,idle_until=utcnow()+timedelta(minutes=30)))
        db.add(AuthBudget(key="c"*64,attempts=5,until=utcnow()+timedelta(minutes=5)))
        db.add(Product(sku="DEMO",name="Demo",brand="Test",reference_price=100,stock=3));db.commit()
        reset_operator(db,new)
        assert matches(new,db.get(Operator,1).password_hash)
        assert not matches(old,db.get(Operator,1).password_hash)
        assert db.query(OperatorSession).count()==db.query(SessionState).count()==db.query(AuthBudget).count()==0
        assert db.query(Product).one().stock==3
        event=db.query(AuditEvent).filter_by(event_type="security.password_recovered").one()
        assert new not in str(event.detail) and old not in str(event.detail)

def test_weak_password_does_not_change_existing_access():
    with SessionLocal() as db:
        db.add(Operator(id=1,password_hash=hash_password("PreviousSecret123!")));db.commit()
        before=db.get(Operator,1).password_hash
        with pytest.raises(ValueError):reset_operator(db,"weak")
        assert db.get(Operator,1).password_hash==before

def test_noninteractive_recovery_is_refused(monkeypatch,capsys):
    monkeypatch.setattr("sys.stdin.isatty",lambda:False)
    assert main()==1
    with SessionLocal() as db:assert db.query(Operator).count()==0
