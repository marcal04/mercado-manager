import base64, hashlib, secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import httpx
from sqlalchemy.orm import Session
from ..config import get_settings
from ..models import MarketplaceConnection
from ..security import decrypt, encrypt
from ..models import Product

AUTH_URL = "https://auth.mercadolivre.com.br/authorization"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
API_URL = "https://api.mercadolibre.com"


class MercadoLivreClient:
    """OAuth and read operations. Reviewed business writes live in listing_manager."""
    def __init__(self, db: Session): self.db, self.settings = db, get_settings()

    def authorization_url(self) -> tuple[str, str, str]:
        if not self.settings.ml_client_id: raise ValueError("ML_CLIENT_ID is not configured")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = urlencode({"response_type": "code", "client_id": self.settings.ml_client_id,
            "redirect_uri": self.settings.ml_redirect_uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        return f"{AUTH_URL}?{query}", state, verifier

    async def exchange_code(self, code: str, verifier: str) -> MarketplaceConnection:
        data = {"grant_type": "authorization_code", "client_id": self.settings.ml_client_id,
            "client_secret": self.settings.ml_client_secret, "code": code,
            "redirect_uri": self.settings.ml_redirect_uri, "code_verifier": verifier}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(TOKEN_URL, data=data, headers={"accept": "application/json"})
            response.raise_for_status(); payload = response.json()
        seller_id = str(payload["user_id"])
        connection = self.db.query(MarketplaceConnection).filter_by(seller_id=seller_id).first()
        if connection is None:
            connection = MarketplaceConnection(seller_id=seller_id,
                access_token_encrypted=encrypt(payload["access_token"]), refresh_token_encrypted=encrypt(payload["refresh_token"]),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=payload["expires_in"]))
            self.db.add(connection)
        else:
            # OAuth codes are single-use; a successful re-authorization must replace
            # both rotating credentials atomically instead of inserting a second row.
            connection.access_token_encrypted = encrypt(payload["access_token"])
            connection.refresh_token_encrypted = encrypt(payload["refresh_token"])
            connection.expires_at = datetime.now(timezone.utc) + timedelta(seconds=payload["expires_in"])
        self.db.commit(); self.db.refresh(connection); return connection

    async def search_public_products(self, query: str) -> dict:
        """Public catalog research; does not require or send a seller token."""
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{API_URL}/sites/{self.settings.ml_site_id}/search", params={"q": query, "limit": 10})
            response.raise_for_status(); return response.json()

    async def import_my_listings(self) -> dict:
        """Imports the seller's listings through GET requests only.

        This is deliberately one-way: it reads Mercado Livre and stores a local
        catalog snapshot; it never writes to an item, price or inventory.
        """
        connection = self.db.query(MarketplaceConnection).order_by(MarketplaceConnection.id.desc()).first()
        if not connection:
            raise ValueError("Connect a Mercado Livre seller before importing listings")
        headers = await self.authorization_headers(connection.id)
        from ..catalog_sync import synchronize
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            async def get(path,params=None):
                response=await client.get(API_URL+path,headers=headers,params=params)
                response.raise_for_status();return response.json()
            outcome=await synchronize(self.db,connection.seller_id,get)
        return {"imported":len(outcome["items"]),**{k:outcome[k] for k in ("total_found","partial","archived","mode")}}

    def _locked_connection(self, connection_id):
        return self.db.query(MarketplaceConnection).filter_by(id=connection_id).populate_existing().with_for_update().one()

    async def authorization_headers(self, connection_id: int) -> dict:
        """Serialize rotating tokens in PostgreSQL; never expose them to callers of the API."""
        from starlette.concurrency import run_in_threadpool
        connection = await run_in_threadpool(self._locked_connection, connection_id)
        expiry = connection.expires_at
        if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=timezone.utc)
        try:
            if expiry <= datetime.now(timezone.utc):
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(TOKEN_URL, data={
                        "grant_type": "refresh_token", "client_id": self.settings.ml_client_id,
                        "client_secret": self.settings.ml_client_secret,
                        "refresh_token": decrypt(connection.refresh_token_encrypted)},
                        headers={"accept": "application/json"})
                    response.raise_for_status()
                    payload = response.json()
                if str(payload["user_id"]) != connection.seller_id:
                    raise ValueError("A renovação retornou outra conta. Reconecte o Mercado Livre.")
                connection.access_token_encrypted = encrypt(payload["access_token"])
                connection.refresh_token_encrypted = encrypt(payload["refresh_token"])
                connection.expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"]))
            token = decrypt(connection.access_token_encrypted)
            self.db.commit()
            return {"Authorization": f"Bearer {token}"}
        except Exception:
            self.db.rollback()
            raise
