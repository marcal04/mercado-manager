"""Create fresh local-only credentials without printing them or overwriting .env."""
import base64
from pathlib import Path
import secrets
from urllib.parse import urlunsplit


def setup(root):
    root=Path(root)
    path=root/".env"
    if path.exists():raise FileExistsError(".env already exists; it was not changed.")
    password=secrets.token_urlsafe(32)
    values={"DATABASE_URL":urlunsplit(("postgresql+psycopg",f"mercado:{password}@db:5432","/mercado_manager","","")),
            "POSTGRES_PASSWORD":password,"RESTORE_CHECK_PASSWORD":secrets.token_urlsafe(32),
            "APP_ENCRYPTION_KEY":base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}
    lines=[]
    for line in (root/".env.example").read_text(encoding="utf-8").splitlines():
        key=line.split("=",1)[0]
        lines.append(key+"="+values[key] if key in values else line)
    # Exclusive creation prevents accidental replacement of existing credentials.
    with path.open("x",encoding="utf-8") as output:output.write("\n".join(lines)+"\n")
    return path

if __name__=="__main__":
    try:
        setup(Path(__file__).resolve().parents[1])
        print("Local .env created with new credentials. No credentials were printed. Do not commit it.")
    except FileExistsError as error:raise SystemExit(str(error))
