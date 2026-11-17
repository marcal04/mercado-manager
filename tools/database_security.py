"""Local Docker maintenance only. Never prints connection passwords or SQL errors."""
import json
import secrets
import subprocess
import sys
from pathlib import Path
from dotenv import dotenv_values, set_key
from psycopg import sql
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
ROLE = "mercado_app"
STAGE = "initialization"


def command(args, content=None):
    result = subprocess.run(args, input=content, text=True, capture_output=True, cwd=ROOT, timeout=120)
    if result.returncode:
        raise RuntimeError("Local database maintenance failed; no secrets were logged.")
    return result.stdout.strip()


def database(statement):
    return command(["docker", "compose", "exec", "-T", "db", "psql", "-X", "-U", "mercado",
                    "-d", "mercado_manager", "-v", "ON_ERROR_STOP=1", "-A", "-t", "-q"], statement)


def schema():
    global STAGE
    STAGE="schema.inspect"
    existing = json.loads(database("SELECT coalesce(json_agg(tablename),'[]'::json) FROM pg_tables WHERE schemaname='public';"))
    enums = json.loads(database("SELECT coalesce(json_agg(t.typname),'[]'::json) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE t.typtype='e' AND n.nspname='public';"))
    # Compile only application-owned SQLAlchemy metadata. No user-provided SQL.
    script = '''import json,sys
from sqlalchemy import create_mock_engine
from app.main import app
from app.db import Base
info=json.load(sys.stdin);known=set(info["tables"]);known_enums=set(info["enums"]);statements=[]
def capture(statement,*args,**kwargs):
    if type(statement).__name__=="CreateEnumType" and statement.element.name in known_enums:return
    statements.append(str(statement.compile(dialect=engine.dialect))+";")
engine=create_mock_engine("postgresql+psycopg://",capture)
missing=[t for t in Base.metadata.sorted_tables if t.name not in known]
if missing:Base.metadata.create_all(engine,tables=missing,checkfirst=False)
print(json.dumps(statements))
'''
    STAGE="schema.compile"
    statements = json.loads(command(["docker", "compose", "run", "--rm", "--no-deps", "-T", "api", "python", "-c", script], json.dumps({"tables":existing,"enums":enums})))
    if statements:
        STAGE="schema.apply"
        database("BEGIN;\n"+"\n".join(statements)+"\nCOMMIT;")


def restrict():
    global STAGE
    STAGE="role.configuration"
    location = ROOT / ".env"
    values = dotenv_values(location)
    connection = make_url(values.get("DATABASE_URL", ""))
    if connection.host != "db" or connection.database != "mercado_manager" or connection.username not in ("mercado", ROLE):
        raise ValueError("This helper only supports the named local Compose installation.")
    role = sql.Identifier(ROLE)
    STAGE="role.inspect"
    exists = database(sql.SQL("SELECT count(*) FROM pg_roles WHERE rolname={};").format(sql.Literal(ROLE)).as_string()) == "1"
    if connection.username != ROLE and exists:
        raise ValueError("The target role already exists; refusing to replace its credentials.")
    password = secrets.token_urlsafe(48) if not exists else None
    statements = [sql.SQL("BEGIN;")]
    if not exists:
        statements.append(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;").format(role, sql.Literal(password)))
    statements.extend([
        sql.SQL("ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;").format(role),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC;"),
        sql.SQL("REVOKE CREATE,TEMPORARY ON DATABASE mercado_manager FROM PUBLIC;"),
        sql.SQL("GRANT CONNECT ON DATABASE mercado_manager TO {};").format(role),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {};").format(role),
        sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {};").format(role),
        sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {};").format(role),
        sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE mercado IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {};").format(role),
        sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE mercado IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO {};").format(role),
        sql.SQL("COMMIT;")])
    STAGE="role.apply"
    database("\n".join(statement.as_string() for statement in statements))
    if password:
        STAGE="role.save"
        updated = connection.set(username=ROLE, password=password).render_as_string(hide_password=False)
        set_key(location, "DATABASE_URL", updated)
    print("Application database role restricted. No credentials were printed.")


if __name__ == "__main__":
    try:
        operation = sys.argv[1] if len(sys.argv) == 2 else ""
        if operation not in {"schema", "restrict"}:
            raise ValueError("Choose schema or restrict")
        schema()
        if operation == "restrict":
            restrict()
        print("Local maintenance completed.")
    except Exception as error:
        print(f"Maintenance stopped at {STAGE} ({type(error).__name__}). No credentials were logged.", file=sys.stderr)
        sys.exit(1)
