"""User-confirmed acquisition basis, independent from unconfirmed selling costs."""
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base, get_db
from .models import Product, now
from .operations import Economics, audit

router = APIRouter(prefix="/api/v1")


class StockProfile(Base):
    __tablename__ = "product_stock_profiles"
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(24))
    acquisition_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProfileInput(BaseModel):
    mode: Literal["owned", "resale"]
    acquisition_cost: Decimal | None = Field(default=None, ge=0, le=10000000, decimal_places=2)


def profile_out(row):
    return {"mode": row.mode, "acquisition_cost": str(row.acquisition_cost) if row.acquisition_cost is not None else None,
            "replenishment_applicable": row.mode == "resale", "updated_at": row.updated_at} if row else None


@router.put("/products/{product_id}/stock-profile")
def save_profile(product_id: int, body: ProfileInput, db: Session = Depends(get_db)):
    item = db.query(Product).filter_by(id=product_id).with_for_update().first()
    if not item:
        raise HTTPException(404, "Produto não encontrado.")
    if body.mode == "owned" and body.acquisition_cost not in (None, Decimal(0)):
        raise HTTPException(422, "Estoque próprio sem compra deve ter custo de aquisição zero.")
    amount = Decimal(0) if body.mode == "owned" else body.acquisition_cost
    economics = db.get(Economics, product_id)
    if economics and amount is None:
        raise HTTPException(422, "Informe o novo custo para atualizar a origem de um produto com custos completos.")
    row = db.get(StockProfile, product_id)
    if not row:
        row = StockProfile(product_id=product_id)
        db.add(row)
    row.mode, row.acquisition_cost, row.updated_at = body.mode, amount, now()
    if economics:
        economics.cost = amount
    audit(db, "stock_profile.updated", "product", product_id,
          {"mode": body.mode, "acquisition_cost": str(amount) if amount is not None else None, "source": "operator"})
    db.commit()
    return profile_out(row)
