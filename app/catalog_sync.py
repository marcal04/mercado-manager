"""Read-only listing reconciliation; archive lifecycle, retain all history/FKs."""
import re
import httpx
from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .models import Product, now
from datetime import datetime
from .operations import audit

class ListingState(Base):
    __tablename__="marketplace_listing_states"
    product_id:Mapped[int]=mapped_column(ForeignKey("products.id"),primary_key=True)
    seller_id:Mapped[str]=mapped_column(String(64),index=True)
    status:Mapped[str]=mapped_column(String(32))
    archived:Mapped[bool]=mapped_column(Boolean,default=False)
    checked_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)


def products_query(db,archived=False):
    archived_ids=db.query(ListingState.product_id).filter(ListingState.archived.is_(True))
    return db.query(Product).filter(Product.id.in_(archived_ids) if archived else ~Product.id.in_(archived_ids))


def observe(db,seller,item):
    if str(item.get("seller_id"))!=str(seller):raise ValueError("Foreign item")
    identity=str(item.get("id",""))
    if not re.fullmatch(r"[A-Z]{3}[0-9]+",identity):raise ValueError("Invalid listing")
    product=db.query(Product).filter_by(sku="meli:"+identity).first()
    brand=next((a.get("value_name") for a in item.get("attributes",[]) if a.get("id")=="BRAND"),None) or "Mercado Livre"
    if not product:
        product=Product(sku="meli:"+identity,name=str(item.get("title") or identity)[:160],brand=str(brand)[:64],reference_price=float(item.get("price") or 0),stock=int(item.get("available_quantity") or 0));db.add(product);db.flush()
    else:
        if item.get("title"):product.name=str(item["title"])[:160]
        product.brand=str(brand)[:64]
        if item.get("price") is not None:product.reference_price=float(item["price"])
        if item.get("available_quantity") is not None:product.stock=int(item["available_quantity"])
    status=str(item.get("status") or "unknown")
    deleted=item.get("deleted") is True or "deleted" in (item.get("sub_status") or [])
    status="deleted" if deleted else status
    row=db.get(ListingState,product.id)
    if not row:row=ListingState(product_id=product.id,seller_id=str(seller),status=status,archived=False);db.add(row)
    elif row.seller_id!=str(seller):raise ValueError("Foreign lifecycle")
    was_archived=bool(row.archived)
    if status in {"closed","deleted"}:row.archived=True
    elif status in {"active","paused","under_review","inactive","payment_required","not_yet_active"}:row.archived=False
    row.status=status;row.checked_at=now()
    if was_archived!=bool(row.archived):audit(db,"catalog.archived" if row.archived else "catalog.restored","product",product.id,{"item_id":identity,"status":status,"source":"Mercado Livre API"})
    return product


async def synchronize(db,seller,get):
    # Lazy import avoids a cycle with the hub that reuses this same reconciler.
    from .data_hub import Snapshot,ProviderError,upsert_snapshot
    errors=[];items=[];seen=set();scroll=None;reported=None;complete=False
    def error(path,e):
        errors.append({"source":path,"http":getattr(e,"code",None) or getattr(getattr(e,"response",None),"status_code",None),"issue":"listing_read"})
    async def read_item(identity):
        path="/items/"+identity
        try:
            item=await get(path)
            if not isinstance(item,dict) or str(item.get("id"))!=identity or str(item.get("seller_id"))!=str(seller):raise ValueError("Foreign or malformed listing")
        except (httpx.HTTPError,ValueError,ProviderError) as e:error(path,e);return None
        observe(db,seller,item)
        upsert_snapshot(db,str(seller),"listings",identity,path,item);db.commit()
        return item
    for page in range(1000):
        path=f"/users/{seller}/items/search"
        try:
            p=await get(path,{"search_type":"scan","limit":100,**({"scroll_id":scroll} if scroll else {})})
            if not isinstance(p,dict) or not isinstance(p.get("results"),list):raise ValueError("Malformed enumeration")
            total=p.get("paging",{}).get("total")
            if not isinstance(total,int) or isinstance(total,bool) or total<0:raise ValueError("Missing enumeration total")
            if reported is not None and total!=reported:raise ValueError("Enumeration changed")
            reported=total;ids=p["results"]
            if any(not isinstance(i,str) or not re.fullmatch(r"[A-Z]{3}[0-9]+",i) for i in ids):raise ValueError("Malformed identifier")
        except (httpx.HTTPError,ValueError,ProviderError) as e:error(path,e);break
        fresh=[i for i in ids if i not in seen]
        if ids and not fresh:break
        for identity in fresh:
            seen.add(identity);item=await read_item(identity)
            if item:items.append(item)
        if len(seen)==reported:
            complete=not errors;break
        if not ids or len(seen)>reported:break
        next_scroll=p.get("scroll_id")
        if not isinstance(next_scroll,str) or not next_scroll or next_scroll==scroll:break
        scroll=next_scroll
    if complete:
        # Absence from search alone is NOT deletion. Confirm each previously owned
        # missing listing individually. Never archive due to 403, timeout or partial scan.
        owned={r.external_id for r in db.query(Snapshot).filter_by(seller_id=str(seller),module="listings")}
        owned.update(p.sku[5:] for p in db.query(Product).join(ListingState,ListingState.product_id==Product.id).filter(ListingState.seller_id==str(seller)))
        for identity in sorted(owned-seen):
            if not re.fullmatch(r"[A-Z]{3}[0-9]+",identity):continue
            product=db.query(Product).filter_by(sku="meli:"+identity).first()
            if not product:continue
            prior=db.get(ListingState,product.id)
            if prior and prior.archived:continue
            item=await read_item(identity)
            if item:continue
            if errors[-1].get("http")!=404:continue
            product=db.query(Product).filter_by(sku="meli:"+identity).first()
            if not product:continue
            row=db.get(ListingState,product.id)
            if row and row.seller_id!=str(seller):continue
            if not row:row=ListingState(product_id=product.id,seller_id=str(seller),archived=False,status="not_found");db.add(row)
            if not row.archived:audit(db,"catalog.archived","product",product.id,{"item_id":identity,"status":"not_found","source":"Mercado Livre API"})
            row.archived=True;row.status="not_found";row.checked_at=now();db.commit();errors.pop()
    archived_now={r.product_id for r in db.query(ListingState).filter_by(seller_id=str(seller),archived=True)}
    return {"items":items,"total_found":reported,"partial":not complete or bool(errors),"errors":errors,
            "archived":len(archived_now),"mode":"read_only"}
