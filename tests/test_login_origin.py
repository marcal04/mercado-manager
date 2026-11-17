import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from app.main import app
from app.config import get_settings
from app.origins import origin_denied,request_origin,secure_request,public_origin

PUBLIC="https://shop.example.test"
PASSWORD="ExampleLoginPass123!"

def request(origin=None,fetch_site=None,host="shop.example.test",extra=None):
    headers={"host":host,**(extra or {})}
    if origin is not None:headers["origin"]=origin
    if fetch_site is not None:headers["sec-fetch-site"]=fetch_site
    return Request({"type":"http","method":"POST","path":"/session","scheme":"http","server":("api",8000),"query_string":b"","headers":[(k.encode(),v.encode()) for k,v in headers.items()]})

@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED","true");monkeypatch.setenv("APP_ACCESS_TOKEN",PASSWORD);monkeypatch.setenv("PUBLIC_BASE_URL",PUBLIC);get_settings.cache_clear()

def test_https_public_origin_is_recognized_over_internal_http(public):
    r=request(PUBLIC,"same-origin")
    assert request_origin(r)==("https","shop.example.test",443)
    assert not origin_denied(r) and secure_request(r)

@pytest.mark.parametrize("origin",["http://shop.example.test","https://shop.example.test:444","https://evil.example.test","null","https://user:secret@shop.example.test","https://shop.example.test:badport"])
def test_other_origins_are_rejected(public,origin):
    assert origin_denied(request(origin,"same-origin"))

def test_fetch_metadata_and_host_are_not_bypassed(public):
    assert origin_denied(request(PUBLIC,"cross-site"))
    assert origin_denied(request(PUBLIC,"same-site"))
    assert origin_denied(request(PUBLIC,"same-origin",host="shop.example.test:8000"))
    assert not secure_request(request(host="127.0.0.1:8000",extra={"x-forwarded-proto":"https","x-forwarded-host":"shop.example.test"}))
    assert not origin_denied(request("http://127.0.0.1:8000","same-origin",host="127.0.0.1:8000"))

def test_public_login_and_session_set_secure_cookies(public):
    with TestClient(app,base_url="http://shop.example.test") as client:
        page=client.get("/login")
        assert page.status_code==200 and "Secure" in page.headers["set-cookie"]
        assert "strict-transport-security" in page.headers
        result=client.post("/session",headers={"origin":PUBLIC,"sec-fetch-site":"same-origin"},json={"password":PASSWORD})
        assert result.status_code==200 and "Secure" in result.headers["set-cookie"]
        assert "HttpOnly" in result.headers["set-cookie"] and "SameSite=strict" in result.headers["set-cookie"]
        # Cookies are consumed on the actual external HTTPS origin.
        assert client.get(PUBLIC+"/api/v1/catalog").status_code==200

def test_proxy_origin_fix_never_skips_password(public):
    with TestClient(app,base_url="http://shop.example.test") as client:
        result=client.post("/session",headers={"origin":PUBLIC,"sec-fetch-site":"same-origin"},json={"password":"incorrect-password"})
        assert result.status_code==401
        assert client.get(PUBLIC+"/api/v1/catalog").status_code==401
        assert client.post("/session",headers={"origin":"https://evil.example.test"},json={"password":PASSWORD}).status_code==403

def test_public_setting_requires_an_https_origin(monkeypatch):
    for value in ["http://shop.example.test","https://shop.example.test/login","https://user:secret@shop.example.test","https://shop.example.test?token=example"]:
        monkeypatch.setenv("PUBLIC_BASE_URL",value);get_settings.cache_clear();assert public_origin() is None
