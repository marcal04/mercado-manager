"""Encrypted PostgreSQL snapshots and isolated restoration verification."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"MMBACKUP1"
CHUNK = 1024 * 1024
ROOT = Path(os.environ.get("BACKUP_DIRECTORY", "/backups"))
INTERVAL = timedelta(hours=24)


def now():
    return datetime.now(timezone.utc)


def key():
    raw = base64.urlsafe_b64decode(os.environ["BACKUP_KEY"])
    if len(raw) != 32:
        raise ValueError("Invalid backup key")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"mercado-manager-backup-v1").derive(raw)


def encrypt(source, target, secret):
    nonce = os.urandom(12)
    context = Cipher(algorithms.AES(secret), modes.GCM(nonce)).encryptor()
    context.authenticate_additional_data(MAGIC)
    with open(source, "rb") as src, open(target, "xb") as dst:
        dst.write(MAGIC + nonce)
        while block := src.read(CHUNK):
            dst.write(context.update(block))
        dst.write(context.finalize())
        dst.write(context.tag)


def decrypt(source, target, secret):
    size = Path(source).stat().st_size
    if size < len(MAGIC) + 12 + 16:
        raise ValueError("Invalid archive")
    created = False
    try:
        with open(source, "rb") as src, open(target, "xb") as dst:
            created = True
            if src.read(len(MAGIC)) != MAGIC:
                raise ValueError("Invalid archive")
            nonce = src.read(12)
            src.seek(-16, 2)
            tag = src.read(16)
            src.seek(len(MAGIC) + 12)
            context = Cipher(algorithms.AES(secret), modes.GCM(nonce, tag)).decryptor()
            context.authenticate_additional_data(MAGIC)
            remaining = size - len(MAGIC) - 12 - 16
            while remaining:
                block = src.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError("Truncated archive")
                remaining -= len(block)
                dst.write(context.update(block))
            # The temporary plaintext must not be consumed before authentication completes.
            dst.write(context.finalize())
    except Exception:
        if created:
            Path(target).unlink(missing_ok=True)
        raise


def read_status():
    try:
        data = json.loads((ROOT / "status.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def status(**changes):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous = read_status()
    previous.update(changes)
    temporary = ROOT / ("status-" + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(previous))
    temporary.replace(ROOT / "status.json")


def run(args, env=None):
    # Never echo raw tool output: errors may contain connection details.
    subprocess.run(args, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)


def counts(connection):
    from psycopg2 import sql
    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        tables = [r[0] for r in cursor.fetchall()]
        result = {}
        for table in tables:
            cursor.execute(sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table)))
            result[table] = cursor.fetchone()[0]
    return result


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(CHUNK):
            result.update(block)
    return result.hexdigest()


def backup():
    import psycopg2
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret = key()
    stamp = now()
    identity = "snapshot-" + stamp.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    pending, target = ROOT / (identity + ".partial"), ROOT / (identity + ".mmbak")
    try:
        with tempfile.TemporaryDirectory(prefix="mm-backup-") as directory:
            workspace = Path(directory)
            dump = workspace / "database.dump"
            # Counts and pg_dump use one consistent snapshot, even during synchronization.
            with psycopg2.connect("") as connection:
                connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_export_snapshot()")
                    snapshot = cursor.fetchone()[0]
                manifest = {"format": 1, "created_at": stamp.isoformat(), "counts": counts(connection)}
                run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--snapshot=" + snapshot, "--file=" + str(dump)])
            run(["pg_restore", "--list", str(dump)])
            manifest["dump_sha256"] = digest(dump)
            (workspace / "manifest.json").write_text(json.dumps(manifest))
            bundle = workspace / "bundle.tar"
            with tarfile.open(bundle, "w") as archive:
                for name in ("manifest.json", "database.dump"):
                    archive.add(workspace / name, arcname=name)
            encrypt(bundle, pending, secret)
            pending.replace(target)
        status(last_backup_at=stamp.isoformat(), next_backup_at=(stamp + INTERVAL).isoformat(),
               last_archive=target.name, bytes=target.stat().st_size, tables=len(manifest["counts"]),
               records=sum(manifest["counts"].values()), backup_state="ok", backup_error=None)
        print("Encrypted backup completed.", flush=True)
        return target
    finally:
        pending.unlink(missing_ok=True)


def unpack(bundle, directory):
    # Only regular files with fixed names, no arbitrary archive paths or links.
    with tarfile.open(bundle, "r:") as archive:
        members = archive.getmembers()
        if sorted(m.name for m in members) != ["database.dump", "manifest.json"] or not all(m.isfile() for m in members):
            raise ValueError("Invalid backup members")
        for member in members:
            with archive.extractfile(member) as src, open(directory / member.name, "xb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("format") != 1 or manifest.get("dump_sha256") != digest(directory / "database.dump"):
        raise ValueError("Invalid backup content")
    return manifest


def verification_environment():
    # This command cannot restore into the running db service, even by changing a CLI flag.
    if os.environ.get("PGHOST") == "restore-check":
        raise ValueError("Source and verification host cannot match")
    return {**os.environ, "PGHOST": "restore-check", "PGDATABASE": "restore_check",
            "PGUSER": "verify", "PGPASSWORD": "local-restore-check", "PGPORT": "5432"}


def verify():
    import psycopg2
    data = read_status()
    name = data.get("last_archive", "")
    if not isinstance(name, str) or not name.startswith("snapshot-") or not name.endswith(".mmbak") or Path(name).name != name:
        raise ValueError("No completed backup available")
    source = ROOT / name
    destination = verification_environment()
    with tempfile.TemporaryDirectory(prefix="mm-verify-") as directory:
        workspace = Path(directory)
        bundle = workspace / "authenticated.tar"
        decrypt(source, bundle, key())
        manifest = unpack(bundle, workspace)
        # Refuse a non-empty target rather than dropping or overwriting any existing data.
        with psycopg2.connect(host=destination["PGHOST"], port=5432, dbname=destination["PGDATABASE"],
                              user=destination["PGUSER"], password=destination["PGPASSWORD"]) as connection:
            if counts(connection):
                raise ValueError("Verification database must be empty")
        run(["pg_restore", "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges",
             "--dbname=restore_check", str(workspace / "database.dump")], env=destination)
        with psycopg2.connect(host=destination["PGHOST"], port=5432, dbname=destination["PGDATABASE"],
                              user=destination["PGUSER"], password=destination["PGPASSWORD"]) as connection:
            restored = counts(connection)
            if restored != manifest["counts"]:
                raise ValueError("Restoration counts differ")
            # Validate actual encrypted application records using the original Fernet key.
            from cryptography.fernet import Fernet
            cipher = Fernet(os.environ["BACKUP_KEY"].encode())
            checked = 0
            with connection.cursor() as cursor:
                for table, columns in (("marketplace_connections", ["access_token_encrypted", "refresh_token_encrypted"]),
                                       ("api_snapshots", ["payload_encrypted"])):
                    if table not in restored:
                        continue
                    from psycopg2 import sql
                    cursor.execute(sql.SQL("SELECT {} FROM public.{}").format(
                        sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(table)))
                    while batch := cursor.fetchmany(100):
                        for row in batch:
                            for value in row:
                                cipher.decrypt(value.encode())
                                checked += 1
        # Do not overwrite backup metadata if a new scheduled snapshot appeared meanwhile.
        status(last_verified_at=now().isoformat(), verified_archive=name, verified_state="ok",
               verified_tables=len(restored), verified_records=sum(restored.values()),
               verified_encrypted_values=checked, verification_error=None)
        print("Isolated restoration verified: counts and encrypted data matched.", flush=True)


def schedule():
    while True:
        data = read_status()
        try:
            last = datetime.fromisoformat(data["last_backup_at"])
            due = now() - last >= INTERVAL
        except (KeyError, ValueError, TypeError):
            due = True
        if due:
            try:
                backup()
            except Exception:
                status(backup_state="failed", backup_error="backup_failed", last_attempt_at=now().isoformat())
                print("Backup failed; retrying later.", flush=True)
                time.sleep(900)
                continue
        time.sleep(30)


if __name__ == "__main__":
    os.umask(0o077)
    command = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        if command == "schedule":
            schedule()
        elif command == "once":
            backup()
        elif command == "verify":
            verify()
        else:
            raise ValueError("Choose schedule, once or verify")
    except Exception:
        if command == "verify":
            status(verified_state="failed", verification_error="verification_failed")
        print("Operation failed. No credentials or database content were logged.", file=sys.stderr)
        sys.exit(1)
