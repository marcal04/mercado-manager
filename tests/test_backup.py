import io
import json
import os
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from app.main import app
from app.config import get_settings
from backup.worker import encrypt, decrypt, unpack, verification_environment

@pytest.mark.parametrize("payload",[b"",b"sensitive data",os.urandom(1048599)])
def test_encrypted_archive_roundtrip_streaming(tmp_path,payload):
    source=tmp_path/"raw";source.write_bytes(payload)
    archive=tmp_path/"archive";output=tmp_path/"out"
    secret=os.urandom(32);encrypt(source,archive,secret)
    assert archive.read_bytes()!=payload
    decrypt(archive,output,secret)
    assert output.read_bytes()==payload

@pytest.mark.parametrize("change",["key","ciphertext","header","truncated"])
def test_archive_authentication_failure_never_leaves_plaintext(tmp_path,change):
    source=tmp_path/"raw";source.write_bytes(b"confidential data"*100)
    archive=tmp_path/"archive";output=tmp_path/"out";secret=os.urandom(32)
    encrypt(source,archive,secret)
    value=bytearray(archive.read_bytes())
    if change=="key":secret=os.urandom(32)
    elif change=="ciphertext":value[30]^=1;archive.write_bytes(value)
    elif change=="header":value[0]^=1;archive.write_bytes(value)
    else:archive.write_bytes(value[:5])
    with pytest.raises((ValueError,InvalidTag)):decrypt(archive,output,secret)
    assert not output.exists()

def test_decryption_cannot_delete_existing_output(tmp_path):
    source=tmp_path/"raw";source.write_bytes(b"data");archive=tmp_path/"archive"
    secret=os.urandom(32);encrypt(source,archive,secret)
    output=tmp_path/"already";output.write_bytes(b"keep")
    with pytest.raises(FileExistsError):decrypt(archive,output,secret)
    assert output.read_bytes()==b"keep"

def test_archive_rejects_path_traversal_and_links(tmp_path):
    bundle=tmp_path/"malicious.tar"
    with tarfile.open(bundle,"w") as archive:
        entry=tarfile.TarInfo("../outside");entry.size=1
        archive.addfile(entry,io.BytesIO(b"x"))
    with pytest.raises(ValueError):unpack(bundle,tmp_path)
    assert not (tmp_path.parent/"outside").exists()

def test_verification_target_is_fixed_and_not_source(monkeypatch):
    monkeypatch.setenv("PGHOST","db");monkeypatch.setenv("PGDATABASE","mercado_manager")
    password=os.urandom(32).hex()
    monkeypatch.setenv("RESTORE_CHECK_PASSWORD",password)
    target=verification_environment()
    assert target["PGPASSWORD"]==password
    assert target["PGHOST"]=="restore-check" and target["PGDATABASE"]=="restore_check"
    monkeypatch.setenv("PGHOST","restore-check")
    with pytest.raises(ValueError):verification_environment()

def test_maintenance_exposes_only_safe_backup_summary(tmp_path,monkeypatch):
    path=tmp_path/"status.json"
    stamp=datetime.now(timezone.utc)
    path.write_text(json.dumps({"last_backup_at":stamp.isoformat(),"next_backup_at":(stamp+timedelta(hours=24)).isoformat(),
        "last_archive":"snapshot-example.mmbak","verified_archive":"snapshot-example.mmbak","last_verified_at":stamp.isoformat(),
        "verified_state":"ok","backup_state":"ok","tables":24,"records":400,"password":"SECRET","backup_error":"SECRET"}))
    monkeypatch.setenv("BACKUP_STATUS_PATH",str(path));get_settings.cache_clear()
    with TestClient(app) as c:
        r=c.get("/api/v1/maintenance");assert r.status_code==200
        v=r.json();assert v["backup_state"]=="ok" and v["latest_archive_verified"]
        assert "SECRET" not in r.text and "snapshot-example" not in r.text
        assert v["tables"]==24 and v["records"]==400
    path.write_text(json.dumps({"last_backup_at":(stamp-timedelta(hours=27)).isoformat()}))
    with TestClient(app) as c:assert c.get("/api/v1/maintenance").json()["backup_state"]=="stale"
    path.write_text("invalid")
    with TestClient(app) as c:assert c.get("/api/v1/maintenance").json()["backup_state"]=="pending"

def test_verification_is_not_misreported_for_newer_archive(tmp_path,monkeypatch):
    path=tmp_path/"status.json"
    stamp=datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps({"last_backup_at":stamp,"last_verified_at":stamp,"verified_state":"ok", "last_archive":"new.mmbak","verified_archive":"old.mmbak"}))
    monkeypatch.setenv("BACKUP_STATUS_PATH",str(path));get_settings.cache_clear()
    with TestClient(app) as c:
        v=c.get("/api/v1/maintenance").json()
        assert v["verification_state"]=="ok" and not v["latest_archive_verified"]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_verification_requires_private_password(monkeypatch,value):
    monkeypatch.setenv("PGHOST","db")
    if value is None:monkeypatch.delenv("RESTORE_CHECK_PASSWORD",raising=False)
    else:monkeypatch.setenv("RESTORE_CHECK_PASSWORD",value)
    with pytest.raises(ValueError,match="RESTORE_CHECK_PASSWORD"):
        verification_environment()
