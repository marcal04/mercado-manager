"""Disposable, offline portfolio demo. Never opens the operational DB or calls a provider."""
import base64
from datetime import timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="mercado-manager-demo-") as directory:
        temporary=Path(directory)
        os.environ.update({"DATABASE_URL":"sqlite:///"+(temporary/"demo.db").as_posix(),
            "APP_ENCRYPTION_KEY":base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            "AUTH_REQUIRED":"false","APP_ACCESS_TOKEN":"","AI_ENABLED":"false","OPENAI_API_KEY":"",
            "AUTO_SYNC_WORKER_ENABLED":"false","MARKETPLACE_WRITE_ENABLED":"false","ML_CLIENT_ID":"","ML_CLIENT_SECRET":"",
            "PUBLIC_BASE_URL":"","ALLOWED_HOSTS":"localhost,127.0.0.1","ML_REDIRECT_URI":"https://shop.example.test/callback",
            "BACKUP_STATUS_PATH":""})
        from app.main import app
        from app.db import Base,engine,SessionLocal
        from app.models import Product,MarketplaceConnection,now
        from app.stock_profile import StockProfile
        from app.seller_proceeds import SellerGoal,context,annotate
        from app.operations import Inventory
        from app.catalog_sync import ListingState
        from app.data_hub import DataJob,MODULES,upsert_snapshot
        from app.marketplace_orders import ImportedOrder,SyncRun,normalize
        from app.auto_sync import AutoSync
        from app.security import encrypt
        from fastapi.responses import HTMLResponse
        import httpx
        import uvicorn
        Base.metadata.create_all(engine)
        seller="999";stamp=now();items={}
        products=[("MLB1001","Hub USB-C 6 em 1",250,24,35,17.5),
                  ("MLB1002","Adaptador de rede USB-C",160,12,20,16),
                  ("MLB1003","Cabo USB-C 2 metros",90,40,12,12)]
        with SessionLocal() as db:
            db.add(MarketplaceConnection(seller_id=seller,access_token_encrypted=encrypt("demo-only-not-a-token"),refresh_token_encrypted=encrypt("demo-only-not-a-token"),expires_at=stamp+timedelta(days=1)))
            for identity,title,price,stock,cost,freight in products:
                product=Product(sku="meli:"+identity,name=title,brand="Demo",reference_price=price,stock=stock);db.add(product);db.flush()
                db.add(StockProfile(product_id=product.id,mode="resale",acquisition_cost=cost))
                db.add(Inventory(product_id=product.id,quantity=stock));db.add(SellerGoal(product_id=product.id,minimum_margin=50))
                db.add(ListingState(product_id=product.id,seller_id=seller,status="active",archived=False))
                item={"id":identity,"title":title,"seller_id":999,"site_id":"MLB","price":price,"currency_id":"BRL","available_quantity":stock,
                      "status":"active","sold_quantity":0,"category_id":"MLB100","listing_type_id":"gold_special","condition":"new",
                      "catalog_listing":False,"tags":[],"variations":[],"pictures":[],"shipping":{"mode":"me2","logistic_type":"drop_off","free_shipping":True}}
                items[identity]=item;ctx=context(item,Decimal(price))
                fee={"listing_type_id":"gold_special","currency_id":"BRL","sale_fee_amount":round(price*.13,2),"sale_fee_details":{"percentage_fee":13,"fixed_fee":0}}
                shipping={"coverage":{"all_country":{"list_cost":freight,"currency_id":"BRL"}}}
                for module,payload in [("listings",item),("fees",annotate(fee,ctx)),("shipping_quotes",annotate(shipping,ctx))]:upsert_snapshot(db,seller,module,identity,"/demo/"+identity,payload)
            order={"id":1001,"seller":{"id":999},"status":"paid","currency_id":"BRL","total_amount":250,
                   "date_created":(stamp-timedelta(days=2)).isoformat(),"date_last_updated":stamp.isoformat(),"pack_id":None,"shipping":{"id":7001},
                   "order_items":[{"item":{"id":"MLB1001","title":"Hub USB-C 6 em 1"},"quantity":1,"unit_price":250,"sale_fee":32.5,"currency_id":"BRL"}],
                   "payments":[{"id":9001,"status":"approved","transaction_amount":90,"marketplace_fee":0,"currency_id":"BRL"},
                               {"id":9002,"status":"approved","transaction_amount":160,"marketplace_fee":0,"currency_id":"BRL"}]}
            db.add(ImportedOrder(**normalize(order,seller)))
            upsert_snapshot(db,seller,"orders","1001","/demo/orders/1001",order)
            upsert_snapshot(db,seller,"shipment_costs","7001","/demo/shipments/7001/costs",{"senders":[{"user_id":999,"cost":17.5}]})
            db.add(SyncRun(seller_id=seller,status="success",days=30,count=1,started_at=stamp,finished_at=stamp,note="Dados fictícios para demonstração"))
            db.add(DataJob(seller_id=seller,status="completed",started_at=stamp,finished_at=stamp,progress={k:{"status":"success","count":0,"errors":[]} for k in MODULES}))
            db.add(AutoSync(seller_id=seller,enabled=False,interval_minutes=60,next_run_at=stamp+timedelta(hours=1),last_started_at=stamp,failures=0))
            upsert_snapshot(db,seller,"management_permissions","application","/demo/permissions",{"listing_write":"unverified","ads_write":"unverified","notice":"Demonstração offline. Escrita externa e IA estão desativadas."})
            db.commit()
        original_client=httpx.AsyncClient
        def mock(request):
            path=request.url.path
            if path=="/users/me":return httpx.Response(200,json={"id":999,"tags":[],"status":{"list":{"allow":False}}})
            if request.method=="GET" and path.startswith("/items/"):
                identity=path.split("/")[2]
                if identity in items and path.count("/")==2:return httpx.Response(200,json=items[identity])
                if identity in items and path.endswith("/description"):return httpx.Response(200,json={"plain_text":"Descrição fictícia para demonstração do produto."})
            return httpx.Response(403,json={"error":"offline_demo","message":"External actions are disabled in the portfolio demo."})
        # Every HTTP client used by the integration receives the in-memory provider.
        httpx.AsyncClient=lambda **kwargs:original_client(transport=httpx.MockTransport(mock),**kwargs)
        app.router.routes[:]=[route for route in app.router.routes if getattr(route,"path",None)!="/"]
        @app.get("/",response_class=HTMLResponse)
        def demo_home():
            html=(ROOT/"app/static/index.html").read_text(encoding="utf-8")
            banner='<div class="card" style="padding:12px 22px;margin-bottom:22px"><b>DEMONSTRAÇÃO</b> · Dados fictícios · Integrações externas desativadas</div>'
            return html.replace('</header>','</header>'+banner).replace('>G<','>M<')
        try:
            print("Demonstração local: http://127.0.0.1:8011 — dados fictícios, sem acesso a serviços externos.")
            uvicorn.run(app,host="127.0.0.1",port=8011,log_level="warning")
        finally:engine.dispose()

if __name__=="__main__":main()
