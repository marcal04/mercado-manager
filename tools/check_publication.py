"""Check publication files without printing secret values. Defense in depth, not a guarantee."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

BLOCKED_DIRS={"backups","sources","private","attachments","uploads","exports","reports","logs"}
SKIP={".git","__pycache__",".pytest_cache",".venv","venv","node_modules"}
BLOCKED_SUFFIX={".db",".sqlite",".sqlite3",".dump",".sql",".mmbak",".pem",".key",".p12",".pfx",".log",".har",".zip"}
PATTERNS={
    "database-password-literal":r"(?m)^[ \t]*(?:POSTGRES_PASSWORD|PGPASSWORD):[ \t]*[A-Za-z0-9][^\r\n]*",
    "private-key":r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "openai-token":r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}",
    "github-token":r"\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})",
    "mercadolivre-token":r"\bAPP_USR-[A-Za-z0-9-]{20,}",
    "aws-access-key":r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "credential-in-url":r"\b(?:postgres(?:ql)?(?:\+psycopg)?|mysql|mongodb(?:\+srv)?)://[^\s\"']+:[^\s@\"']+@",
}


def git(root,*args):
    r=subprocess.run(["git","-C",str(root),*args],capture_output=True)
    if r.returncode:raise RuntimeError("Cannot inspect the Git index; no secret data was logged.")
    return r.stdout


def env_values(path):
    result=[]
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key,sep,value=line.partition("=")
        if not sep or key.lstrip().startswith("#"):continue
        value=value.strip().strip("\"'")
        if any(word in key.upper() for word in ("KEY","SECRET","TOKEN","PASSWORD","DATABASE_URL","CLIENT_ID","REDIRECT","PUBLIC_BASE_URL")) and len(value)>=8:
            result.append(value)
    return result


def scan(root,staged=False,compare_env=None):
    root=Path(root).resolve();findings=[]
    if staged:
        paths=[Path(p.decode()) for p in git(root,"diff","--cached","--name-only","--diff-filter=ACMR","-z").split(b"\0") if p]
    elif (root/".git").exists():
        paths=[Path(p.decode()) for p in git(root,"ls-files","--cached","-z").split(b"\0") if p]
    else:
        paths=[p.relative_to(root) for p in root.rglob("*") if p.is_file() and not set(p.relative_to(root).parts)&SKIP]
    secrets=env_values(compare_env) if compare_env else []
    approved_images={}
    try:
        manifest_path=Path("docs/images/manifest.json")
        raw_manifest=git(root,"show",":"+manifest_path.as_posix()) if staged else (root/manifest_path).read_bytes()
        approved_images=json.loads(raw_manifest).get("images",{})
    except (OSError,ValueError,RuntimeError):pass
    for rel in paths:
        path=root/rel;parts={p.lower() for p in rel.parts};name=rel.name.lower()
        blocked=(parts&BLOCKED_DIRS or (name.startswith(".env") and name!=".env.example") or path.suffix.lower() in BLOCKED_SUFFIX or name.endswith(".env") or ".db-" in name or ".sqlite-" in name)
        if blocked:findings.append((rel.as_posix(),0,"private-or-generated-file"));continue
        if path.is_symlink():findings.append((rel.as_posix(),0,"symlink"));continue
        content=git(root,"show",":"+rel.as_posix()) if staged else path.read_bytes()
        try:text=content.decode("utf-8")
        except UnicodeDecodeError:
            reviewed=approved_images.get(rel.as_posix(),{})
            if not (rel.as_posix().startswith("docs/images/") and rel.suffix.lower()==".jpg" and content.startswith(bytes([255,216,255])) and reviewed.get("privacy_review")=="synthetic_data_only" and reviewed.get("sha256")==hashlib.sha256(content).hexdigest()):
                findings.append((rel.as_posix(),0,"unreviewed-binary"))
            continue
        if name==".env.example":
            private_keys={"DATABASE_URL","POSTGRES_PASSWORD","RESTORE_CHECK_PASSWORD","APP_ENCRYPTION_KEY","OPENAI_API_KEY","ML_CLIENT_ID","ML_CLIENT_SECRET","APP_ACCESS_TOKEN","PUBLIC_BASE_URL"}
            for line_no,line in enumerate(text.splitlines(),1):
                key,sep,val=line.partition("=")
                if sep and key.strip() in private_keys and val.strip().strip("\"'"):
                    findings.append((rel.as_posix(),line_no,"nonempty-private-template-field"))
        for category,pattern in PATTERNS.items():
            for match in re.finditer(pattern,text):findings.append((rel.as_posix(),text.count("\n",0,match.start())+1,category))
        for value in secrets:
            if value in text:findings.append((rel.as_posix(),0,"matches-operational-env"))
    return len(paths),sorted(set(findings))

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--staged",action="store_true");parser.add_argument("--root",default=str(Path(__file__).resolve().parents[1]));parser.add_argument("--compare-env")
    args=parser.parse_args()
    try:count,findings=scan(args.root,args.staged,args.compare_env)
    except (OSError,RuntimeError) as error:raise SystemExit(str(error))
    for path,line,category in findings:print(f"{path}:{line}: {category}")
    print(f"Reviewed {count} files; publication findings: {len(findings)}. Values are never printed.")
    sys.exit(bool(findings))
