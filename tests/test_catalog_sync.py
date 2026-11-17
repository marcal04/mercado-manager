import asyncio,json
from datetime import timedelta
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import Product,MarketplaceConnection,now
from app.security import encrypt
from app.config import get_settings
from app.catalog_sync import synchronize,ListingState,observe,products_query
from app.data_hub import upsert_snapshot
from app.operations import Inventory,Economics,Order,catalog
from app.services.mercadolivre import MercadoLivreClient

@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv('APP_ENCRYPTION_KEY',Fernet.generate_key().decode());get_settings.cache_clear()
    with SessionLocal() as db:
        db.add(MarketplaceConnection(seller_id='123',access_token_encrypted=encrypt('fake'),refresh_token_encrypted=encrypt('fake'),expires_at=now()+timedelta(hours=1)));db.commit()

def item(id,status='active',seller=123):
    return {'id':id,'seller_id':seller,'title':id,'price':200,'available_quantity':3,'status':status,'attributes':[]}

def seed(db,id='MLB1',seller='123'):
    p=observe(db,seller,item(id,seller=int(seller)));upsert_snapshot(db,seller,'listings',id,'/items/'+id,item(id,seller=int(seller)));db.commit();return p.id

def run(db,handler):
    async def get(path,params=None):
        r=handler(path,params or {})
        response=httpx.Response(r[0],json=r[1],request=httpx.Request('GET','https://api.mercadolibre.com'+path));response.raise_for_status();return response.json()
    return asyncio.run(synchronize(db,'123',get))

def test_closed_deleted_disappear_but_history_and_local_stock_survive(connected):
    with SessionLocal() as db:
        id=seed(db);db.add(Inventory(product_id=id,quantity=8));db.add(Order(reference='historic',product_id=id,quantity=1,unit_price=200,status='completed'));db.add(Economics(product_id=id,cost=0,fee_percent=10,shipping=10,tax_percent=0,minimum_margin=50,minimum_stock=0));db.commit()
        def api(path,q):
            if 'search' in path:return 200,{'results':['MLB1','MLB2'],'paging':{'total':2}}
            return 200,({**item('MLB1','closed'),'sub_status':['deleted']} if path.endswith('MLB1') else item('MLB2'))
        result=run(db,api);assert not result['partial'] and result['archived']==1
        assert [p['sku'] for p in catalog(db)]==['meli:MLB2']
        assert db.get(Product,id) and db.get(Inventory,id).quantity==8 and db.get(Economics,id).cost==0 and db.query(Order).count()==1
        with TestClient(app) as c:
            assert len(c.get('/api/v1/catalog').json())==1
            archived=c.get('/api/v1/catalog/archived').json();assert archived[0]['marketplace_status']=='deleted'
            assert c.get('/api/v1/store-dashboard').json()['products']==1
            assert len(c.get('/api/v1/seller-proceeds').json())==1
            assert len(c.get('/api/v1/replenishment/plan').json()['results'])==1
            assert len(c.get('/api/v1/products').json())==1

def test_absence_in_complete_search_is_individually_confirmed(connected):
    calls=[]
    with SessionLocal() as db:
        old=seed(db)
        def api(path,q):
            calls.append(path)
            if 'search' in path:return 200,{'results':['MLB2'],'paging':{'total':1}}
            return 200,item('MLB1','closed') if path.endswith('MLB1') else item('MLB2')
        result=run(db,api);assert '/items/MLB1' in calls and db.get(ListingState,old).archived
        assert not result['partial']

@pytest.mark.parametrize('failure',[403,500,'timeout'])
def test_missing_item_denial_or_network_failure_does_not_archive(connected,failure):
    with SessionLocal() as db:
        old=seed(db)
        def api(path,q):
            if 'search' in path:return 200,{'results':[],'paging':{'total':0}}
            if failure=='timeout':raise httpx.ReadTimeout('test')
            return failure,{}
        result=run(db,api);assert result['partial'] and not db.get(ListingState,old).archived
        assert len(catalog(db))==1

def test_confirmed_404_only_archives_previously_owned_products(connected):
    with SessionLocal() as db:
        old=seed(db);foreign=seed(db,'MLB9','999')
        def api(path,q):return (200,{'results':[],'paging':{'total':0}}) if 'search' in path else (404,{})
        result=run(db,api);assert db.get(ListingState,old).archived and not db.get(ListingState,foreign).archived
        assert not result['partial']

@pytest.mark.parametrize('mode',['denied','no_cursor','repeated_cursor','changed_total','unknown_total'])
def test_incomplete_scan_never_archives_unseen_items(connected,mode):
    count=0
    with SessionLocal() as db:
        old=seed(db)
        def api(path,q):
            nonlocal count
            if 'search' not in path:return 200,item('MLB2')
            count+=1
            if mode=='denied':return 403,{}
            if mode=='unknown_total':return 200,{'results':['MLB2']}
            total=3 if mode=='changed_total' and count>1 else 2
            return 200,{'results':['MLB2'],'paging':{'total':total},**({} if mode=='no_cursor' else {'scroll_id':'same'})}
        result=run(db,api);assert result['partial'] and not db.get(ListingState,old).archived

def test_paused_stays_visible_and_reactivation_restores_existing_row(connected):
    with SessionLocal() as db:
        old=seed(db);observe(db,'123',item('MLB1','closed'));db.commit();assert not catalog(db)
        observe(db,'123',item('MLB1','paused'));db.commit();assert catalog(db)[0]['id']==old
        assert not db.get(ListingState,old).archived and db.query(Product).count()==1

def test_all_pages_are_read_before_reconciling_and_manual_import_uses_same_logic(connected,monkeypatch):
    calls=[]
    def handler(r):
        assert r.method=='GET';calls.append(r)
        if 'search' in r.url.path:
            second=r.url.params.get('scroll_id')=='next'
            return httpx.Response(200,json={'results':['MLB2' if second else 'MLB1'],'paging':{'total':2},'scroll_id':'next'})
        return httpx.Response(200,json=item(r.url.path.split('/')[-1],'paused'))
    original=httpx.AsyncClient;monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    with SessionLocal() as db:
        result=asyncio.run(MercadoLivreClient(db).import_my_listings())
        assert result['imported']==2 and not result['partial'] and len(catalog(db))==2
    assert any(r.url.params.get('scroll_id')=='next' for r in calls)

def test_local_products_and_foreign_owned_data_are_never_archived(connected):
    with SessionLocal() as db:
        foreign=seed(db,'MLB9','999');p=Product(sku='LOCAL1',name='Local',brand='Test',reference_price=20,stock=2);db.add(p);db.commit()
        run(db,lambda path,q:(200,{'results':[],'paging':{'total':0}}))
        assert len(catalog(db))==2 and not db.get(ListingState,foreign).archived
