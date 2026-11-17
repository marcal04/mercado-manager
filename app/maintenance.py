"""Read-only local backup health; never exposes archives or keys."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import APIRouter
from .config import get_settings

router = APIRouter(prefix="/api/v1")


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo else None
    except (ValueError, TypeError):
        return None


@router.get("/maintenance")
def backup_status():
    location = get_settings().backup_status_path
    data = {}
    try:
        path = Path(location)
        if location and path.stat().st_size <= 16384:
            value = json.loads(path.read_text())
            if isinstance(value, dict):
                data = value
    except (OSError, ValueError):
        pass
    last = timestamp(data.get("last_backup_at"))
    verified = timestamp(data.get("last_verified_at"))
    current = datetime.now(timezone.utc)
    state = "not_configured" if not location else "pending" if not last else "stale" if current - last > timedelta(hours=26) else "ok"
    if data.get("backup_state") == "failed":
        state = "failed"
    same = bool(verified and data.get("verified_archive") == data.get("last_archive"))
    return {"backup_state": state, "last_backup_at": last, "next_backup_at": timestamp(data.get("next_backup_at")),
            "last_verified_at": verified, "latest_archive_verified": same and data.get("verified_state") == "ok",
            "verification_state": "failed" if data.get("verified_state") == "failed" else "ok" if verified else "pending",
            "tables": data.get("tables") if type(data.get("tables")) is int else None,
            "records": data.get("records") if type(data.get("records")) is int else None,
            "scope": "local_encrypted_copy"}
