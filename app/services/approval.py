from datetime import datetime, timezone
from sqlalchemy.orm import Session
from ..models import Approval, ApprovalStatus, AuditEvent


class ApprovalService:
    def __init__(self, db: Session): self.db = db

    def request(self, action_type: str, payload: dict, risk_note: str) -> Approval:
        approval = Approval(action_type=action_type, payload=payload, risk_note=risk_note)
        self.db.add(approval); self.db.flush()
        self._audit("approval.requested", approval, {"action_type": action_type})
        self.db.commit(); self.db.refresh(approval)
        return approval

    def decide(self, approval: Approval, approve: bool, actor: str) -> Approval:
        if approval.status != ApprovalStatus.pending:
            raise ValueError("This approval has already been decided")
        status = ApprovalStatus.approved if approve else ApprovalStatus.rejected
        changed = self.db.query(Approval).filter(Approval.id == approval.id, Approval.status == ApprovalStatus.pending).update(
            {Approval.status: status, Approval.decided_at: datetime.now(timezone.utc), Approval.decided_by: actor}, synchronize_session=False)
        if changed != 1:
            self.db.rollback()
            raise ValueError("This approval has already been decided")
        self.db.refresh(approval)
        self._audit(f"approval.{approval.status.value}", approval, {"actor": actor, "simulated": True})
        self.db.commit(); self.db.refresh(approval)
        return approval

    def _audit(self, event_type: str, approval: Approval, detail: dict):
        self.db.add(AuditEvent(event_type=event_type, entity_type="approval", entity_id=str(approval.id), detail=detail))
