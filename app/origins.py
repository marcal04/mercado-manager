"""Trusted public origin for HTTPS tunnels; never trust arbitrary forwarded headers."""
from urllib.parse import urlsplit
from .config import get_settings


def identity(value):
    try:
        parsed=urlsplit(value)
        if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:return None
        return parsed.scheme.lower(),parsed.hostname.lower(),parsed.port or (443 if parsed.scheme=="https" else 80)
    except (ValueError,TypeError):return None


def public_origin():
    value=get_settings().public_base_url
    if not value:return None
    try:
        parsed=urlsplit(value)
        origin=identity(value)
        if not origin or origin[0]!="https" or parsed.path not in {"","/"} or parsed.query or parsed.fragment:return None
        return origin
    except (ValueError,TypeError):return None


def request_origin(request):
    actual=identity(str(request.url))
    configured=public_origin()
    if configured and actual:
        host=urlsplit("//"+request.headers.get("host", ""))
        try:
            # Hostname AND external port must match the explicit server setting.
            # An internal HTTP request with Host example.test means HTTPS port 443
            # only for this configured host, never for arbitrary domains.
            if host.hostname and host.hostname.lower()==configured[1] and (host.port or 443)==configured[2]:
                return configured
        except ValueError:pass
    return actual


def secure_request(request):
    origin=request_origin(request)
    return bool(origin and origin[0]=="https")


def origin_denied(request):
    if request.headers.get("sec-fetch-site") in {"cross-site","same-site"}:return True
    value=request.headers.get("origin") or request.headers.get("referer")
    if not value:return False
    origin=identity(value)
    return origin is None or origin!=request_origin(request)
