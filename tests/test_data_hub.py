import asyncio
import json
from datetime import timedelta
from urllib.parse import parse_qs
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.config import get_settings
from app.models import MarketplaceConnection,Product,now
from app.operations import Economics,Inventory
from app.data_hub import Snapshot,DataJob,Hub,upsert_snapshot,clean
from app.security import encrypt,decrypt

@pytest.fixture(autouse=True)
def fast_mock_billing(monkeypatch):
    monkeypatch.setattr('app.data_hub.BILLING_GAP_SECONDS',0)

@pytest.fixture
def connection(monkeypatch):
    monkeypatch.setenv('APP_ENCRYPTION_KEY',Fernet.generate_key().decode());get_settings.cache_clear()
    with SessionLocal() as db:
        db.add(MarketplaceConnection(seller_id='123',access_token_encrypted=encrypt('fake-access'),refresh_token_encrypted=encrypt('fake-refresh'),expires_at=now()+timedelta(hours=1)));db.commit()

def order(id=10):
    return {'id':id,'seller':{'id':123},'status':'paid','currency_id':'BRL','total_amount':100,'date_created':now().isoformat(),'date_last_updated':now().isoformat(),'pack_id':8,'shipping':{'id':7},'order_items':[{'item':{'id':'MLB1','title':'Product'},'quantity':1,'unit_price':100,'sale_fee':12}],'buyer':{'name':'PRIVATE BUYER'},'access_token':'MUST NOT STORE'}

def provider(requests):
    def handle(r):
        requests.append(r);assert r.method=='GET'
        path=r.url.path
        if path=='/users/me':return httpx.Response(200,json={'id':123,'nickname':'Loja teste','email':'PRIVATE EMAIL','password':'DROP THIS'})
        if path=='/users/123/items/search':return httpx.Response(200,json={'results':['MLB1'],'paging':{'total':1}})
        if path=='/items/MLB1':return httpx.Response(200,json={'id':'MLB1','seller_id':123,'title':'Product','site_id':'MLB','price':100,'available_quantity':9,'currency_id':'BRL','category_id':'MLB123','listing_type_id':'gold_special','shipping':{'mode':'me2','logistic_type':'fulfillment'},'inventory_id':'INV1','variations':[]})
        if path=='/items/MLB1/description':return httpx.Response(200,json={'plain_text':'Descrição completa'})
        if path=='/items/MLB1/prices':return httpx.Response(200,json={'prices':[{'amount':100}]})
        if path=='/sites/MLB/listing_prices':return httpx.Response(200,json=[{'listing_type_id':'gold_special','sale_fee_amount':12,'sale_fee_details':{'percentage_fee':10,'fixed_fee':2}}])
        if path=='/users/123/shipping_options/free':return httpx.Response(200,json={'coverage':{'all_country':{'list_cost':15}}})
        if 'visits' in path:return httpx.Response(200,json={'total_visits':31})
        if path=='/questions/search':
            offset=int(r.url.params['offset']);return httpx.Response(200,json={'questions':[{'id':offset+1,'text':'Pergunta'}],'total':2})
        if path=='/orders/search':return httpx.Response(200,json={'results':[order()],'paging':{'total':1}})
        if path=='/orders/10':return httpx.Response(200,json=order())
        if path in ('/shipments/7','/shipments/7/costs'):return httpx.Response(200,json={'id':7,'status':'delivered','senders':[{'user_id':123,'cost':15}]})
        if path=='/messages/packs/8/sellers/123':
            assert r.url.params['mark_as_read']=='false';return httpx.Response(200,json={'messages':[{'id':'message1','text':'Mensagem privada'}],'paging':{'total':1}})
        if path=='/post-purchase/v1/claims/search':return httpx.Response(200,json={'data':[{'id':55,'type':'return','players':[{'user_id':123,'role':'respondent'}]}],'paging':{'total':1}})
        if path=='/post-purchase/v1/claims/55':return httpx.Response(200,json={'id':55,'type':'return','status':'closed'})
        if path=='/seller-promotions/users/123':return httpx.Response(200,json={'results':[],'paging':{'total':0}})
        if path=='/inventories/INV1/stock/fulfillment':return httpx.Response(200,json={'inventory_id':'INV1','available_quantity':9})
        if path.startswith('/categories/'):return httpx.Response(200,json={'id':'MLB123'} if not path.endswith('/attributes') else [])
        if path=='/billing/integration/monthly/periods':return httpx.Response(403,json={'error':'secret-provider-error'})
        if path=='/advertising/advertisers':return httpx.Response(404,json={'error':'secret-provider-error'})
        return httpx.Response(404,json={})
    return handle

def mock_client(monkeypatch,handler):
    real=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:real(transport=httpx.MockTransport(handler),**kwargs))

def test_full_sync_encrypted_idempotent_and_read_only(connection,monkeypatch):
    requests=[];mock_client(monkeypatch,provider(requests))
    with TestClient(app) as c:
        assert c.post('/api/v1/data-hub/sync').status_code==202
        status=c.get('/api/v1/data-hub/status').json();assert status['job']['status']=='completed_with_gaps'
        assert len(status['job']['progress'])==21
        assert status['job']['progress']['billing']['status']=='denied'
        assert status['job']['progress']['ads']['status']=='unavailable'
        assert status['counts']['questions']==2 and status['counts']['orders']==1
        facts=c.get('/api/v1/data-hub/product-facts').json()[0]
        assert facts['fee_amount_estimate']==12 and facts['fee_fixed_estimate']==2 and facts['fee_percent_estimate']==10
        assert facts['available_quantity']==9 and facts['visits_90_days']==31
        records=c.get('/api/v1/data-hub/records?module=account').json()
        id=records['results'][0]['id']
        assert c.get(f'/api/v1/data-hub/records/{id}').status_code==409
        assert c.post('/api/v1/data-hub/sync').status_code==202
        assert c.get('/api/v1/data-hub/status').json()['counts']==status['counts']
    with SessionLocal() as db:
        row=db.query(Snapshot).filter_by(module='account').one()
        assert 'PRIVATE EMAIL' not in row.payload_encrypted
        data=json.loads(decrypt(row.payload_encrypted));assert data['email']=='PRIVATE EMAIL' and 'password' not in data
        order_payload=json.loads(decrypt(db.query(Snapshot).filter_by(module='orders').one().payload_encrypted));assert 'access_token' not in order_payload
        assert db.query(Product).one().stock==9 and db.query(Inventory).count()==0 and db.query(Economics).count()==0
    assert all(r.method=='GET' for r in requests)

def test_record_details_require_login_and_are_account_scoped(connection,monkeypatch):
    with TestClient(app) as c:
        with SessionLocal() as db:
            upsert_snapshot(db,'123','account','123','/users/me',{'id':123});db.commit()
        monkeypatch.setenv('APP_ACCESS_TOKEN','test-password');get_settings.cache_clear()
        assert c.get('/api/v1/data-hub/records/1').status_code==401
        c.post('/session',json={'password':'test-password'})
        assert c.get('/api/v1/data-hub/records/1').json()['data']['id']==123
        with SessionLocal() as db:
            db.add(MarketplaceConnection(seller_id='999',access_token_encrypted=encrypt('fake'),refresh_token_encrypted=encrypt('fake'),expires_at=now()));db.commit()
        assert c.get('/api/v1/data-hub/records/1').status_code==404
        assert c.get('/api/v1/data-hub/records?module=account').json()['total']==0

def test_existing_catalog_and_manual_values_preserved(connection,monkeypatch):
    mock_client(monkeypatch,provider([]))
    with TestClient(app) as c:
        p=c.post('/api/v1/products',json={'sku':'meli:MLB1','name':'Before','brand':'Test','reference_price':50,'stock':2}).json()['id']
        c.put(f'/api/v1/products/{p}/economics',json={'cost':40,'fee_percent':10,'shipping':2,'tax_percent':0,'minimum_margin':20,'minimum_stock':3})
        c.post(f'/api/v1/products/{p}/stock',json={'delta':4,'reason':'Conferido','actor':'Operador'})
        c.post('/api/v1/data-hub/sync')
        prod=c.get('/api/v1/catalog').json()[0]
        assert prod['price']==100 and prod['marketplace_stock']==9 and prod['local_stock']==4 and prod['economics']['cost']==40

def test_no_connection_and_invalid_resource():
    with TestClient(app) as c:
        assert c.post('/api/v1/data-hub/sync').status_code==409
        assert c.get('/api/v1/data-hub/records?module=../../oauth/token').status_code==422

def test_cancel_and_single_running_job(connection):
    with TestClient(app) as c:
        with SessionLocal() as db:
            j=DataJob(seller_id='123',status='running',progress={});db.add(j);db.commit()
        assert c.post('/api/v1/data-hub/sync').status_code==409
        assert c.post('/api/v1/data-hub/cancel').status_code==200
        with SessionLocal() as db:
            job=db.query(DataJob).one();hub=Hub(db,job,{},None)
            with pytest.raises(asyncio.CancelledError):hub.check()

def test_recursive_secret_scrubbing():
    assert clean({'safe':[{'client_secret':'x','access_token':'y','nested':{'password':'z','ok':1}}]})=={'safe':[{'nested':{'ok':1}}]}


def test_marketplace_stock_can_feed_replenishment_without_physical_count(connection,monkeypatch):
    mock_client(monkeypatch,provider([]))
    with TestClient(app) as c:
        c.post('/api/v1/data-hub/sync')
        r=c.get('/api/v1/replenishment/plan?stock_source=marketplace').json()['results'][0]
        assert r['stock_base']==9 and r['physical_stock'] is None and r['stock_source']=='marketplace'
        assert not any('estoque físico' in b for b in r['blockers'])
        assert r['stock_fetched_at']
        assert c.get('/api/v1/replenishment/plan?stock_source=arbitrary').status_code==422

def test_dependency_denied_and_old_snapshot_preserved(connection,monkeypatch):
    def denied(r):
        if r.url.path=='/users/123/items/search':return httpx.Response(403,json={})
        return httpx.Response(404,json={})
    mock_client(monkeypatch,denied)
    with TestClient(app) as c:
        with SessionLocal() as db:
            upsert_snapshot(db,'123','listings','MLB1','/items/MLB1',{'id':'MLB1','seller_id':123,'price':99});db.commit()
        c.post('/api/v1/data-hub/sync')
        status=c.get('/api/v1/data-hub/status').json()
        assert status['job']['progress']['listings']['status']=='denied'
        assert status['job']['progress']['descriptions']['status']=='blocked'
        assert status['counts']['listings']==1


def test_billing_cursor_and_daily_cache(connection):
    requests=[]
    def handler(r):
        requests.append(r)
        if r.url.path.endswith('/monthly/periods'):return httpx.Response(200,json={'total':1,'results':[{'key':'2026-11-01'}]})
        if r.url.path.endswith('/documents'):return httpx.Response(200,json={'total':0,'results':[]})
        if r.url.path.endswith('/summary/details'):
            assert r.url.params['document_type'] in ('BILL','CREDIT_NOTE')
            return httpx.Response(200,json={'bill_includes':{'total_amount':100}})
        assert 'from_id' in r.url.params and 'offset' not in r.url.params
        assert r.url.params['document_type'] in ('BILL','CREDIT_NOTE')
        return httpx.Response(200,json={'results':[{'id':1}],'last_id':1} if r.url.params['from_id']=='0' else {'results':[]})
    async def perform():
        with SessionLocal() as db:
            job=DataJob(seller_id='123',status='running',progress={});db.add(job);db.commit()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                hub=Hub(db,job,{'Authorization':'Bearer fake'},client);hub.module='billing'
                await hub.run_billing();first=len(requests)
                assert first==20 and not hub.errors
                await hub.run_billing();assert len(requests)==first
    asyncio.run(perform())

def test_large_order_window_is_split_instead_of_truncated(connection):
    from datetime import datetime
    searches=[]
    def handler(r):
        if r.url.path=='/orders/search':
            a=datetime.fromisoformat(r.url.params['order.date_created.from']);b=datetime.fromisoformat(r.url.params['order.date_created.to']);searches.append((a,b))
            if b-a>timedelta(days=300):return httpx.Response(200,json={'results':[],'paging':{'total':1001}})
            id=10 if a==searches[0][0] else 11
            return httpx.Response(200,json={'results':[order(id)],'paging':{'total':1}})
        return httpx.Response(200,json=order(int(r.url.path.rsplit('/',1)[-1])))
    async def perform():
        with SessionLocal() as db:
            job=DataJob(seller_id='123',status='running',progress={});db.add(job);db.commit()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                hub=Hub(db,job,{'Authorization':'Bearer fake'},client);hub.module='orders'
                await hub.run_orders()
                assert len(searches)==3 and len(hub.orders)==2 and not hub.partial
    asyncio.run(perform())


def test_claims_from_an_unrelated_account_are_not_stored(connection,monkeypatch):
    base=provider([])
    def handler(r):
        if r.url.path=='/post-purchase/v1/claims/search':return httpx.Response(200,json={'data':[{'id':999,'type':'return','players':[{'user_id':999,'role':'respondent'}]}],'paging':{'total':1}})
        return base(r)
    mock_client(monkeypatch,handler)
    with TestClient(app) as c:
        c.post('/api/v1/data-hub/sync')
        status=c.get('/api/v1/data-hub/status').json()
        assert status['counts'].get('claims',0)==0 and status['job']['progress']['claims']['status']=='failed'
