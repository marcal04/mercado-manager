import copy,json
from datetime import timedelta
from decimal import Decimal
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from app.main import app
from app.db import SessionLocal
from app.config import get_settings
from app.models import Product,MarketplaceConnection,now
from app.stock_profile import StockProfile
from app.seller_proceeds import SellerGoal
from app.security import encrypt
from app.listing_manager import ListingChange,Picture,capabilities

@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv('APP_ENCRYPTION_KEY',Fernet.generate_key().decode());monkeypatch.setenv('MARKETPLACE_WRITE_ENABLED','true');get_settings.cache_clear()
    item={'id':'MLB123','seller_id':123,'title':'Produto original','price':200,'currency_id':'BRL','site_id':'MLB','category_id':'MLB10','listing_type_id':'gold_special','available_quantity':3,'sold_quantity':0,'status':'active','catalog_listing':True,'user_product_id':'MLBU123','family_name':'Produto','shipping':{'mode':'me2','logistic_type':'drop_off','free_shipping':True},'tags':[],'pictures':[{'id':'picture-1'},{'id':'picture-2'}],'variations':[]}
    user={'id':123,'tags':['user_product_seller'],'status':{'list':{'allow':True}}}
    w={'item':item,'user':user,'calls':[],'mutations':[],'description':None,'fail':None,'validate_fail':False}
    def handler(r):
        w['calls'].append(r);path=r.url.path
        if path=='/applications/12345':return httpx.Response(200,json={'scopes':w.get('scopes',['urn:ml:mktp:publish-sync:/read-write'])})
        if path=='/users/me':return httpx.Response(200,json=w['user'])
        if path.startswith('/pricing-automation/'):return httpx.Response(404,json={})
        if path=='/items/validate':return httpx.Response(400 if w['validate_fail'] else 204,json={'cause':[{'code':'item.attribute.missing_required'}]} if w['validate_fail'] else None)
        if path=='/categories/MLB10':return httpx.Response(200,json={'name':'Categoria','settings':{'max_title_length':60}})
        if path.endswith('/listing_prices'):return httpx.Response(200,json={'listing_type_id':'gold_special','currency_id':'BRL','sale_fee_amount':26})
        if path.endswith('/shipping_options/free'):return httpx.Response(200,json={'coverage':{'all_country':{'currency_id':'BRL','list_cost':24}}})
        if path in ['/items/MLB123','/items/MLB999','/items'] and r.method in ['PUT','POST']:
            w['mutations'].append(r)
            if w['fail']=='timeout':raise httpx.ReadTimeout('secret-access-token',request=r)
            if w['fail']=='403':return httpx.Response(403,json={'message':'secret-access-token','cause':[]})
            data=json.loads(r.content)
            if path=='/items':w['item']={**w['item'],**data,'id':'MLB999','title':'Novo produto'}
            else:w['item'].update(data)
            return httpx.Response(201 if path=='/items' else 200,json=w['item'])
        if path.endswith('/description'):
            if r.method=='GET':return httpx.Response(404 if w['description'] is None else 200,json={} if w['description'] is None else {'plain_text':w['description']})
            w['mutations'].append(r)
            if w['fail']=='description':return httpx.Response(400,json={})
            w['description']=json.loads(r.content)['plain_text'];return httpx.Response(200,json={'plain_text':w['description']})
        if path in ['/items/MLB123','/items/MLB999']:return httpx.Response(200,json=w['item'])
        raise AssertionError((r.method,path))
    original=httpx.AsyncClient
    monkeypatch.setattr('app.listing_manager.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    with SessionLocal() as db:
        p=Product(sku='meli:MLB123',name='Produto',brand='Test',reference_price=200,stock=3);db.add(p);db.flush();w['product_id']=p.id
        db.add(StockProfile(product_id=p.id,mode='owned',acquisition_cost=0));db.add(SellerGoal(product_id=p.id,minimum_margin=50))
        db.add(MarketplaceConnection(seller_id='123',access_token_encrypted=encrypt('fake-access'),refresh_token_encrypted=encrypt('fake-refresh'),expires_at=now()+timedelta(hours=1)));db.commit()
    return w

def draft(c,w,operation='quantity',**kwargs):
    body={'operation':operation,'reason':'Solicitação do operador',**({operation:5} if not kwargs else kwargs)}
    return c.post('/api/v1/listing-manager/products/'+str(w['product_id'])+'/plans',json=body)

def execute(c,p,**kwargs):return c.post(f"/api/v1/listing-manager/plans/{p['id']}/execute",json={'review_hash':p['review_hash'],**kwargs})

def test_prepare_is_read_only_and_execution_requires_exact_review(world):
    with TestClient(app) as c:
        r=draft(c,world);assert r.status_code==200;r=r.json();assert not world['mutations']
        assert execute(c,{**r,'review_hash':'0'*64}).status_code==409 and not world['mutations']
        result=execute(c,r);assert result.status_code==200 and result.json()['state']=='succeeded'
        assert len(world['mutations'])==1
        assert execute(c,r).status_code==409 and len(world['mutations'])==1
        with SessionLocal() as db:assert db.get(Product,world['product_id']).stock==5

def test_changed_stock_invalidates_approval(world):
    with TestClient(app) as c:
        p=draft(c,world).json();world['item']['available_quantity']=2
        assert execute(c,p).json()['state']=='conflict' and not world['mutations']

def test_expired_rejected_and_flag_off_never_write(world,monkeypatch):
    with TestClient(app) as c:
        p=draft(c,world).json()
        with SessionLocal() as db:db.get(ListingChange,p['id']).expires_at=now()-timedelta(seconds=1);db.commit()
        assert execute(c,p).json()['state']=='expired'
        p=draft(c,world).json();assert c.post(f"/api/v1/listing-manager/plans/{p['id']}/reject",json={'review_hash':p['review_hash']}).json()['state']=='rejected'
        assert execute(c,p).status_code==409
        p=draft(c,world).json();monkeypatch.setenv('MARKETPLACE_WRITE_ENABLED','false');get_settings.cache_clear()
        assert execute(c,p).status_code==409 and not world['mutations']

def test_catalog_and_user_product_restrictions(world):
    caps=capabilities(world['item'],world['user']);assert caps['price'] and caps['quantity'] and caps['status']
    assert not caps['title'] and not caps['pictures'] and not caps['description']
    with TestClient(app) as c:
        assert draft(c,world,'title',title='Outro título').status_code==409
        assert draft(c,world,'pictures',pictures=[{'id':'picture-2'}]).status_code==409
        world['item']['tags']=['dynamic_standard_price'];assert draft(c,world,'price',price=210).status_code==409
        world['user']['tags'].append('warehouse_management');assert draft(c,world).status_code==409
    assert not world['mutations']

def test_price_uses_api_margin_and_rechecks_before_execution(world,monkeypatch):
    with TestClient(app) as c:
        p=draft(c,world,'price',price=250).json();assert p['margin']['remaining_estimate']=='200.00'
        with SessionLocal() as db:db.get(SellerGoal,world['product_id']).minimum_margin=90;db.commit()
        assert execute(c,p).json()['state']=='conflict' and not world['mutations']
        assert draft(c,world,'price',price=60).status_code==409

def test_price_put_and_pause_success(world):
    with TestClient(app) as c:
        p=draft(c,world,'price',price=250).json();assert execute(c,p).json()['state']=='succeeded'
        p=draft(c,world,'status',status='paused').json();assert execute(c,p).json()['state']=='succeeded'
        assert len(world['mutations'])==2

def test_timeout_unknown_is_not_retried_and_reconcile_get_only(world):
    with TestClient(app) as c:
        p=draft(c,world).json();world['fail']='timeout';r=execute(c,p)
        assert r.json()['state']=='unknown' and 'secret-access-token' not in r.text
        assert execute(c,p).status_code==409 and len(world['mutations'])==1
        r=c.post(f"/api/v1/listing-manager/plans/{p['id']}/verify")
        assert r.json()['state']=='sent' and len(world['mutations'])==1

def test_permission_denial_is_clear_and_not_retried(world):
    with TestClient(app) as c:
        p=draft(c,world).json();world['fail']='403';r=execute(c,p)
        assert r.json()['state']=='failed' and 'permissões' in r.json()['note'] and 'secret-access-token' not in r.text
        assert execute(c,p).status_code==409 and len(world['mutations'])==1

PUBLISH={'title':'Cabo USB-C Apple','category_id':'MLB10','price':200,'quantity':2,'pictures':[{'source':'https://example.com/photo.jpg'}],'description':'Produto original novo','attributes':[{'id':'BRAND','value_name':'Apple'}],'reason':'Novo produto do estoque'}

def test_new_product_validates_then_creates_once_and_description_separately(world):
    with TestClient(app) as c:
        r=c.post('/api/v1/listing-manager/publish/plans',json=PUBLISH);assert r.status_code==200,payload_error(r)
        p=r.json();assert not world['mutations'] and 'family_name' in p['after'] and 'title' not in p['after']
        r=execute(c,p);assert r.json()['state']=='succeeded',r.text
        assert r.json()['item_id']=='MLB999' and len(world['mutations'])==2
        assert execute(c,p).status_code==409 and len(world['mutations'])==2
        with SessionLocal() as db:assert db.query(Product).filter_by(sku='meli:MLB999').count()==1

def payload_error(r):return r.text

def test_partial_creation_never_reposts_item(world):
    with TestClient(app) as c:
        p=c.post('/api/v1/listing-manager/publish/plans',json=PUBLISH).json();world['fail']='description'
        r=execute(c,p);assert r.json()['state']=='partial' and r.json()['item_id']=='MLB999'
        assert execute(c,p).status_code==409
        c.post(f"/api/v1/listing-manager/plans/{p['id']}/verify")
        assert len([r for r in world['mutations'] if r.url.path=='/items'])==1

def test_validator_error_blocks_publish_plan(world):
    world['validate_fail']=True
    with TestClient(app) as c:
        r=c.post('/api/v1/listing-manager/publish/plans',json=PUBLISH)
        assert r.status_code==422 and 'item.attribute.missing_required' in r.text and not world['mutations']
    with SessionLocal() as db:assert db.query(ListingChange).count()==0

def test_cross_seller_rejected_and_no_arbitrary_payload(world):
    with TestClient(app) as c:
        world['item']['seller_id']=999;assert draft(c,world).status_code==403
        assert draft(c,world,path='/users/999',quantity=5).status_code==422
        assert draft(c,world,quantity=5,price=1).status_code==422
    assert not world['mutations']

@pytest.mark.parametrize('url',['http://example.com/a.jpg','https://127.0.0.1/a','https://169.254.169.254/a','https://user:password@example.com/a','https://host.local/a'])
def test_private_and_credential_image_urls_refused(url):
    with pytest.raises(ValueError):Picture(source=url)

def test_shared_user_product_notice_and_traditional_description(world):
    world['item'].update(catalog_listing=False,family_name=None,user_product_id=None)
    with TestClient(app) as c:
        p=draft(c,world,'description',description='Descrição revisada').json();assert execute(c,p).json()['state']=='succeeded'
        assert world['mutations'][0].method=='POST'
        p=draft(c,world,'description',description='Segunda descrição').json();assert execute(c,p).json()['state']=='succeeded'
        assert world['mutations'][1].method=='PUT'
        world['item']['sold_quantity']=1;assert draft(c,world,'title',title='Novo título').status_code==409

def test_execution_claim_is_unique_per_seller(world):
    with TestClient(app) as c:
        a=draft(c,world).json();b=draft(c,world,'status',status='paused').json()
        with SessionLocal() as db:db.get(ListingChange,a['id']).state='executing';db.commit()
        assert execute(c,b).status_code==409 and not world['mutations']

def test_management_requires_login(monkeypatch):
    monkeypatch.setenv('AUTH_REQUIRED','true');get_settings.cache_clear()
    with TestClient(app) as c:
        for path in ['/status','/plans','/products/1']:
            assert c.get('/api/v1/listing-manager'+path).status_code==401
        assert c.post('/api/v1/listing-manager/plans/1/execute',json={'review_hash':'0'*64}).status_code==401

def test_granular_read_only_blocks_even_with_general_write(world,monkeypatch):
    monkeypatch.setenv('ML_CLIENT_ID','12345');get_settings.cache_clear()
    world['scopes']=['write','read','urn:ml:mktp:publish-sync:/read-only','urn:ml:mktp:ads:/read-only']
    with TestClient(app) as c:
        p=draft(c,world).json()
        permissions=c.post('/api/v1/listing-manager/permissions/check').json()
        assert permissions['listing_write']=='read_only' and permissions['ads_write']=='read_only'
        assert execute(c,p).json()['state']=='failed' and not world['mutations']
        assert c.get('/api/v1/listing-manager/status').json()['permissions']['listing_write']=='read_only'

def test_configured_granular_write_allows_reviewed_action(world,monkeypatch):
    monkeypatch.setenv('ML_CLIENT_ID','12345');get_settings.cache_clear()
    with TestClient(app) as c:
        p=draft(c,world).json();assert execute(c,p).json()['state']=='succeeded'

def test_verification_cannot_release_running_execution(world):
    with TestClient(app) as c:
        p=draft(c,world).json()
        with SessionLocal() as db:
            row=db.get(ListingChange,p['id']);row.state='executing';row.decided_at=now();db.commit()
        assert c.post(f"/api/v1/listing-manager/plans/{p['id']}/verify").status_code==409
        assert not world['mutations']

def test_interrupted_creation_remains_unknown_without_reposting(world):
    with TestClient(app) as c:
        p=c.post('/api/v1/listing-manager/publish/plans',json=PUBLISH).json()
        with SessionLocal() as db:
            row=db.get(ListingChange,p['id']);row.state='executing';row.decided_at=now()-timedelta(minutes=6);db.commit()
        assert c.post(f"/api/v1/listing-manager/plans/{p['id']}/verify").json()['state']=='unknown'
        assert not world['mutations']
