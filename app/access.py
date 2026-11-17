"""Single-operator sign-in. Passwords are never stored or returned as plaintext."""
import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import DateTime, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from starlette.concurrency import run_in_threadpool
from .config import get_settings
from .db import Base, SessionLocal
from .auth_guard import limited, clear_limit, activate, active, TOKEN_PATTERN, ATTEMPT_COOKIE, SessionState, issue_browser_cookie
from .models import AuditEvent
from .origins import origin_denied, secure_request, public_origin

class Operator(Base):
    __tablename__ = "operator_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(256))

class OperatorSession(Base):
    __tablename__ = "operator_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

COOKIE = "manager_session"
PUBLIC = {"/login", "/session", "/session/setup", "/health", "/api/v1/integrations/mercadolivre/callback", "/api/v1/webhooks/mercadolivre"}


def hash_password(password):
    salt = secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=3, maxmem=67108864)
    return "scrypt-32768-8-3$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(result).decode()

def matches(password, encoded):
    try:
        kind, salt, expected = encoded.split("$")
        if kind != "scrypt-32768-8-3": return False
        result = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=32768, r=8, p=3, maxmem=67108864)
        return hmac.compare_digest(result, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError): return False

def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()

def state(token=None, agent=""):
    with SessionLocal() as db:
        configured = bool(get_settings().app_access_token or db.get(Operator, 1))
        row = db.get(OperatorSession, token_hash(token)) if token and TOKEN_PATTERN.fullmatch(token) else None
        expiry = row.expires_at.replace(tzinfo=timezone.utc) if row and row.expires_at.tzinfo is None else row.expires_at if row else None
        return configured, bool(expiry and expiry > datetime.now(timezone.utc) and active(db, token_hash(token), agent))

def protected_access_configured(db):
    return bool(get_settings().app_access_token or db.get(Operator, 1))

def issue_session(db, request):
    stamp = datetime.now(timezone.utc)
    db.query(OperatorSession).delete()
    token = secrets.token_urlsafe(32)
    db.add(OperatorSession(token_hash=token_hash(token), expires_at=stamp+timedelta(hours=12)))
    activate(db, token_hash(token), request)
    db.add(AuditEvent(event_type="security.login_success",entity_type="operator",entity_id="1",detail={}))
    db.commit()
    response = JSONResponse({"ok": True})
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=43200, secure=secure_request(request))
    response.delete_cookie(ATTEMPT_COOKIE)
    return response

def strong_password(value):
    return 12<=len(value)<=128 and any(c.isalpha() for c in value) and any(c.isdecimal() for c in value) and any(not c.isalnum() and not c.isspace() for c in value)

def record_failure():
    with SessionLocal() as db:
        db.add(AuditEvent(event_type="security.login_failed",entity_type="operator",entity_id="1",detail={}))
        db.commit()

async def password_body(request, change=False):
    raw = bytearray()
    async for block in request.stream():
        raw.extend(block)
        if len(raw)>4096: return None
    try:
        value=json.loads(raw)
        allowed={"current_password","new_password","confirmation"} if change else {"password","confirmation"}
        field="new_password" if change else "password"
        if not isinstance(value,dict) or set(value)-allowed or not isinstance(value.get(field),str) or not 1<=len(value[field])<=128: return None
        if change and (not isinstance(value.get("current_password"),str) or not 1<=len(value["current_password"])<=128):return None
        return value
    except (ValueError, UnicodeDecodeError): return None

def login_page(setup, nonce):
    title="Crie seu acesso" if setup else "Bem-vindo de volta"
    note="Use pelo menos 12 caracteres, com letras, números e símbolos. Este acesso é só seu." if setup else "Entre para acompanhar sua operação."
    confirmation='<label>Confirme a senha<input id="confirmation" type="password" autocomplete="new-password" required minlength="12" maxlength="128"></label>' if setup else ''
    html="""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Acesso · Mercado Manager</title><style>body{margin:0;background:#f5feff;color:#0e2f76;font:16px system-ui;display:grid;min-height:100vh;place-items:center}main{width:min(360px,80vw);padding:38px;background:#ffffffc9;border:1px solid #aac0e155;border-radius:30px;box-shadow:0 20px 70px #0e2f7612}h1{font-size:28px;letter-spacing:-1px}p{color:#556988;line-height:1.6}label{display:block;margin:18px 0;font-size:13px}input{box-sizing:border-box;display:block;width:100%;padding:14px;margin-top:8px;border:1px solid #aac0e1;border-radius:12px;font:inherit}button{width:100%;padding:15px;border:0;border-radius:14px;color:white;background:#0e2f76;font:inherit;cursor:pointer}small{color:#556988}#error{font-size:13px}</style></head><body><main><small>MERCADO MANAGER · SEU ESPAÇO</small><h1>__TITLE__</h1><p>__NOTE__</p><form id="form"><label>Sua senha<input id="password" type="password" autocomplete="__AUTOCOMPLETE__" required maxlength="128" __MINIMUM__></label>__CONFIRMATION__<button id="submit">__BUTTON__</button></form><p id="error" role="alert"></p></main><script nonce="__NONCE__">const form=document.getElementById('form');form.onsubmit=async e=>{e.preventDefault();const button=document.getElementById('submit');button.disabled=true;try{let body={password:document.getElementById('password').value};const confirm=document.getElementById('confirmation');if(confirm)body.confirmation=confirm.value;const r=await fetch('__PATH__',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});if(r.ok){location.replace('/');return;}const data=await r.json();document.getElementById('error').textContent=typeof data.detail==='string'?data.detail:'Não foi possível entrar.';}catch{document.getElementById('error').textContent='Não foi possível conectar. Tente novamente.';}finally{button.disabled=false;}};</script></body></html>"""
    replacements={"__TITLE__":title,"__NOTE__":note,"__CONFIRMATION__":confirmation,"__NONCE__":nonce,"__AUTOCOMPLETE__":"new-password" if setup else "current-password","__MINIMUM__":'minlength="12"' if setup else '',"__BUTTON__":"Criar meu acesso" if setup else "Entrar","__PATH__":"/session/setup" if setup else "/session"}
    for key,value in replacements.items(): html=html.replace(key,value)
    return html

def install_access(app):
    @app.middleware("http")
    async def gate(request:Request,call_next):
        nonce=secrets.token_urlsafe(24);request.state.csp_nonce=nonce
        async def dispatch():
            settings=get_settings()
            allowed={x.strip().lower() for x in settings.allowed_hosts.split(',') if x.strip()}
            callback_host=urlsplit(settings.ml_redirect_uri).hostname
            if callback_host: allowed.add(callback_host.lower())
            public=public_origin()
            if public:allowed.add(public[1])
            if request.url.hostname not in allowed:
                return JSONResponse({"detail":"Endereço não permitido."},status_code=400)
            if request.method not in {"GET","HEAD","OPTIONS"} and request.url.path!="/api/v1/webhooks/mercadolivre":
                if origin_denied(request):
                    return JSONResponse({"detail":"Origem não permitida. Reabra a página de entrada no mesmo endereço do painel."},status_code=403)
            configured,authenticated=await run_in_threadpool(state,request.cookies.get(COOKIE),request.headers.get("user-agent",""))
            if (settings.auth_required or configured) and request.url.path not in PUBLIC and not authenticated:
                if await run_in_threadpool(limited,request,'denied'):return JSONResponse({'detail':'Muitas solicitações. Aguarde um minuto.'},status_code=429,headers={'Retry-After':'60'})
                if request.url.path=='/':return RedirectResponse('/login',status_code=303)
                return JSONResponse({"detail":"Entre no painel para continuar."},status_code=401)
            return await call_next(request)
        response=await dispatch()
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']=f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if secure_request(request):response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    @app.get('/login',response_class=HTMLResponse)
    async def login(request:Request):
        configured,_=await run_in_threadpool(state)
        return issue_browser_cookie(HTMLResponse(login_page(not configured,request.state.csp_nonce)),request)

    @app.post('/session/setup')
    async def setup(request:Request):
        if request.url.hostname not in {'localhost','127.0.0.1','::1'}:
            return JSONResponse({"detail":"Crie o acesso pelo endereço local do computador."},status_code=403)
        configured,_=await run_in_threadpool(state)
        if configured:return JSONResponse({"detail":"O acesso já foi configurado."},status_code=409)
        if await run_in_threadpool(limited,request):return JSONResponse({"detail":"Muitas tentativas. Aguarde 5 minutos."},status_code=429,headers={"Retry-After":"300"})
        body=await password_body(request)
        if not body or not strong_password(body['password']) or body['password']!=body.get('confirmation'):
            return JSONResponse({"detail":"Use pelo menos 12 caracteres com letras, números e símbolos, e confirme a mesma senha."},status_code=422)
        encoded=await run_in_threadpool(hash_password,body['password'])
        with SessionLocal() as db:
            if protected_access_configured(db):return JSONResponse({"detail":"O acesso já foi configurado."},status_code=409)
            db.add(Operator(id=1,password_hash=encoded))
            db.add(AuditEvent(event_type="security.operator_created",entity_type="operator",entity_id="1",detail={}))
            try:db.commit()
            except IntegrityError:
                db.rollback();return JSONResponse({"detail":"O acesso já foi configurado."},status_code=409)
            await run_in_threadpool(clear_limit,request)
            return issue_session(db,request)

    @app.post('/session')
    async def session(request:Request):
        if await run_in_threadpool(limited,request):return JSONResponse({"detail":"Muitas tentativas. Aguarde 5 minutos."},status_code=429,headers={"Retry-After":"300"})
        body=await password_body(request)
        if not body:
            await run_in_threadpool(record_failure)
            return JSONResponse({"detail":"Acesso inválido."},status_code=401)
        with SessionLocal() as db:
            account=db.query(Operator).filter_by(id=1).with_for_update().first()
            valid=await run_in_threadpool(matches,body['password'],account.password_hash) if account else bool(get_settings().app_access_token) and hmac.compare_digest(body['password'].encode(),get_settings().app_access_token.encode())
            if not valid:
                await run_in_threadpool(record_failure)
                return JSONResponse({"detail":"Acesso inválido."},status_code=401)
            await run_in_threadpool(clear_limit,request)
            return issue_session(db,request)

    @app.post('/session/password')
    async def change_password(request:Request):
        if await run_in_threadpool(limited,request):return JSONResponse({"detail":"Muitas tentativas. Aguarde 5 minutos."},status_code=429,headers={"Retry-After":"300"})
        body=await password_body(request,change=True)
        if not body or not strong_password(body['new_password']) or body['new_password']!=body.get('confirmation') or body['new_password']==body['current_password']:
            return JSONResponse({"detail":"Escolha uma nova senha com 12 ou mais caracteres, letras, números e símbolos, e confirme."},status_code=422)
        with SessionLocal() as db:
            account=db.query(Operator).filter_by(id=1).with_for_update().first()
            valid=await run_in_threadpool(matches,body['current_password'],account.password_hash) if account else bool(get_settings().app_access_token) and hmac.compare_digest(body['current_password'].encode(),get_settings().app_access_token.encode())
            if not valid:
                await run_in_threadpool(record_failure)
                return JSONResponse({"detail":"Não foi possível confirmar o acesso."},status_code=401)
            encoded=await run_in_threadpool(hash_password,body['new_password'])
            if account:account.password_hash=encoded
            else:db.add(Operator(id=1,password_hash=encoded))
            db.add(AuditEvent(event_type="security.password_changed",entity_type="operator",entity_id="1",detail={}))
            await run_in_threadpool(clear_limit,request)
            return issue_session(db,request)

    @app.delete('/session')
    def logout(request:Request):
        token=request.cookies.get(COOKIE)
        if token and TOKEN_PATTERN.fullmatch(token):
            with SessionLocal() as db:
                db.query(OperatorSession).filter_by(token_hash=token_hash(token)).delete()
                db.query(SessionState).filter_by(token_hash=token_hash(token)).delete()
                db.add(AuditEvent(event_type="security.logout",entity_type="operator",entity_id="1",detail={}));db.commit()
        response=JSONResponse({"ok":True});response.delete_cookie(COOKIE);return response
