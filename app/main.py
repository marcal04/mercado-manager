from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
import secrets
import logging
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from .config import get_settings
from .db import Base, engine, get_db
from .models import Approval, ApprovalStatus, Product
from .services.ai import AIService
from .services.approval import ApprovalService
from .services.mercadolivre import MercadoLivreClient

from .operations import router as operations_router

oauth_pending: dict[str, str] = {}
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    # This Compose deployment uses one API worker. Recover interrupted background syncs.
    from .db import SessionLocal
    with SessionLocal() as db:
        db.query(DataJob).filter_by(status="running").update({DataJob.status:"interrupted"})
        db.commit()
    task = asyncio.create_task(auto_sync_worker()) if get_settings().auto_sync_worker_enabled else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

app = FastAPI(title="AI Mercado Manager", version="0.1.0", lifespan=lifespan,
              docs_url="/docs" if get_settings().api_docs_enabled else None,
              redoc_url=None, openapi_url="/openapi.json" if get_settings().api_docs_enabled else None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

from .access import install_access
install_access(app)
from .marketplace_orders import router as marketplace_orders_router
app.include_router(marketplace_orders_router)
from .pricing import router as pricing_router
from .replenishment import router as replenishment_router
app.include_router(replenishment_router)
app.include_router(pricing_router)
from .data_hub import router as data_hub_router,DataJob
from .api_completion import router as api_details_router
app.include_router(api_details_router)
app.include_router(data_hub_router)
from .auto_sync import router as auto_sync_router, worker as auto_sync_worker
app.include_router(auto_sync_router)
app.include_router(operations_router)
from .stock_profile import router as stock_profile_router
app.include_router(stock_profile_router)
from .maintenance import router as maintenance_router
app.include_router(maintenance_router)
from .store_dashboard import router as store_dashboard_router
app.include_router(store_dashboard_router)
from .seller_proceeds import router as seller_proceeds_router
app.include_router(seller_proceeds_router)

from .catalog_sync import ListingState,products_query
from .listing_manager import router as listing_manager_router
app.include_router(listing_manager_router)

class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)
class ApprovalDecision(BaseModel): approved: bool; actor: str = Field(min_length=2, max_length=120)
class ProductInput(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    brand: str = Field(min_length=1, max_length=64)
    reference_price: float = Field(gt=0, le=10000000, allow_inf_nan=False)
    stock: int = Field(ge=0, le=1000000)

def approval_out(a: Approval):
    return {"id": a.id, "action_type": a.action_type, "payload": a.payload, "risk_note": a.risk_note,
            "status": a.status.value, "requested_at": a.requested_at, "decided_at": a.decided_at, "decided_by": a.decided_by}

@app.get("/", response_class=HTMLResponse)
def dashboard(): return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

@app.get("/health")
def health(): return {"status": "ok", "marketplace_write_enabled": get_settings().marketplace_write_enabled}

@app.get("/api/v1/products")
def list_products(db: Session = Depends(get_db)):
    return [{"id": p.id, "sku": p.sku, "name": p.name, "brand": p.brand, "reference_price": p.reference_price, "stock": p.stock} for p in products_query(db).all()]

@app.post("/api/v1/products", status_code=201)
def create_product(body: ProductInput, db: Session = Depends(get_db)):
    if db.query(Product).filter_by(sku=body.sku).first(): raise HTTPException(409, "SKU already exists")
    product = Product(**body.model_dump()); db.add(product); db.commit(); db.refresh(product)
    return {"id": product.id, **body.model_dump()}

@app.post("/api/v1/chat")
def chat(body: ChatRequest, db: Session = Depends(get_db)):
    return AIService(db).chat(body.message, [item.model_dump() for item in body.history])

@app.get("/api/v1/approvals")
def list_approvals(db: Session = Depends(get_db)):
    return [approval_out(a) for a in db.query(Approval).order_by(Approval.id.desc()).all()]

@app.post("/api/v1/approvals/{approval_id}/decision")
def decide(approval_id: int, body: ApprovalDecision, db: Session = Depends(get_db)):
    approval = db.get(Approval, approval_id)
    if not approval: raise HTTPException(404, "Approval not found")
    try: return approval_out(ApprovalService(db).decide(approval, body.approved, body.actor))
    except ValueError as error: raise HTTPException(409, str(error))

@app.get("/api/v1/integrations/mercadolivre/authorize")
def ml_authorize(db: Session = Depends(get_db)):
    try:
        url, state, verifier = MercadoLivreClient(db).authorization_url(); oauth_pending[state] = verifier
        return RedirectResponse(url)
    except ValueError as error: raise HTTPException(503, str(error))

@app.get("/api/v1/integrations/mercadolivre/callback")
async def ml_callback(code: str, state: str, db: Session = Depends(get_db)):
    verifier = oauth_pending.pop(state, None)
    if not verifier: raise HTTPException(400, "Invalid or expired OAuth state")
    try:
        connection = await MercadoLivreClient(db).exchange_code(code, verifier)
    except Exception: raise HTTPException(502, "Mercado Livre authorization exchange failed")
    return {"connected": True, "seller_id": connection.seller_id, "message": "Tokens are encrypted at rest and are never returned."}

@app.get("/api/v1/research/mercadolivre")
async def research(query: str, db: Session = Depends(get_db)):
    return await MercadoLivreClient(db).search_public_products(query)

@app.post("/api/v1/integrations/mercadolivre/sync-listings")
async def sync_listings(db: Session = Depends(get_db)):
    """Read and import existing seller listings. No Mercado Livre writes occur."""
    try:
        return await MercadoLivreClient(db).import_my_listings()
    except ValueError as error:
        raise HTTPException(409, str(error))
    except httpx.HTTPStatusError as error:
        logger.exception("Mercado Livre listing import failed with HTTP %s", error.response.status_code)
        raise HTTPException(502, f"Mercado Livre returned HTTP {error.response.status_code} while reading listings. Check the Docker log; no data was changed.")
    except Exception:
        logger.exception("Mercado Livre listing import failed")
        raise HTTPException(502, "Could not read listings from Mercado Livre. Check the Docker log; no data was changed.")

