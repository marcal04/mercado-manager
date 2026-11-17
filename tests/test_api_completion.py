import asyncio,json
from datetime import timedelta
import httpx
import pytest
from cryptography.fernet import Fernet
from app.config import get_settings
from app.db import SessionLocal
from app.data_hub import DataJob,Hub,Snapshot,upsert_snapshot
from app.models import MarketplaceConnection,now
from app.security import encrypt,decrypt
from app.api_completion import collect_descriptions,collect_claim_details,collect_ads_metrics,finance_view,billing_view,advertising_view

@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv('APP_ENCRYPTION_KEY',Fernet.generate_key().decode());get_settings.cache_clear()
    with SessionLocal() as db:
        db.add(MarketplaceConnection(seller_id='123',access_token_encrypted=encrypt('test'),refresh_token_encrypted=encrypt('test'),expires_at=now()+timedelta(hours=1)));db.commit()

def collect(function,module,handler,items=None,claims=None):
    async def perform():
        with SessionLocal() as db:
            job=DataJob(seller_id='123',status='running',progress={});db.add(job);db.commit()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                hub=Hub(db,job,{'Authorization':'Bearer fake'},client);hub.module=module;hub.items=items or [];hub.claims=claims or []
                await function(hub);return hub.errors
    return asyncio.run(perform())

def test_catalog_fallback_is_labeled_and_unsafe_html_removed(connected):
    def handler(r):
        assert r.method=='GET'
        if r.url.path.startswith('/items/'):return httpx.Response(404,json={'error':'not_found'})
        return httpx.Response(200,json={'short_description':{'type':'html','content':'<p>Produto &amp; original</p><script>EVIL()</script>'}})
    errors=collect(collect_descriptions,'descriptions',handler,items=[{'id':'MLB1','title':'Teste','catalog_product_id':'MLBPROD'},{'id':'MLB2','title':'Sem texto'}]);assert errors==[]
    with SessionLocal() as db:
        values={r.external_id:json.loads(decrypt(r.payload_encrypted)) for r in db.query(Snapshot)}
        assert values['MLB1']['origin']=='catalog' and values['MLB1']['listing_description_present'] is False
        assert values['MLB1']['plain_text']=='Produto & original' and 'EVIL' not in str(values)
        assert values['MLB2']['origin']=='absent'

def test_restricted_buyer_claim_is_not_reported_as_seller_claim(connected):
    requests=[]
    def handler(r):requests.append(r);return httpx.Response(403,json={'message':'not allowed'})
    errors=collect(collect_claim_details,'claims',handler,claims=[{'id':1,'players':[{'user_id':123,'role':'complainant'}]}])
    assert len(requests)==1 and errors[0]['http']==403
    with SessionLocal() as db:
        info=json.loads(decrypt(db.query(Snapshot).one().payload_encrypted))
        assert info['state']=='restricted' and info['account_roles']==['complainant']

def test_pads_metrics_use_current_paths_and_api_version(connected):
    with SessionLocal() as db:
        upsert_snapshot(db,'123','ads','PADS','/advertising/advertisers',{'advertisers':[{'advertiser_id':9,'site_id':'MLB'}]});db.commit()
    paths=[]
    def handler(r):
        paths.append(r.url.path);assert r.method=='GET' and r.headers['api-version']=='2'
        assert 'date_from' in r.url.params and 'date_to' in r.url.params
        if r.url.path.endswith('/campaigns/search'):return httpx.Response(200,json={'results':[{'id':10,'metrics':{'clicks':3}}],'paging':{'total':1}})
        if r.url.path.endswith('/ad_groups/search'):return httpx.Response(200,json={'results':[{'ad_group_id':20}],'paging':{'total':1}})
        if r.url.path.endswith('/ads'):return httpx.Response(200,json={'results':[{'id':'MLB1','metrics':{'clicks':3}}],'paging':{'total':1}})
        return httpx.Response(200,json={'id':20,'metrics':{'cost':5}})
    assert collect(collect_ads_metrics,'ads_metrics',handler)==[]
    assert '/advertising/MLB/product_ads/campaigns/10' in paths
    assert '/advertising/MLB/product_ads/ad_groups/20/ads' in paths
    assert not any(p.startswith('/advertising/product_ads/') for p in paths)

def seed_orders(db,grouped=True,missing_fee=False,mixed_currency=False):
    payment={'id':5,'status':'approved','transaction_amount':200 if grouped else 100,'marketplace_fee':None if missing_fee else 26 if grouped else 13,'currency_id':'BRL'}
    for id in [1,2]:
        pay=dict(payment)
        if not grouped:pay['id']=id
        o={'id':id,'seller':{'id':123},'pack_id':9 if grouped else None,'currency_id':'USD' if mixed_currency and id==2 else 'BRL','total_amount':100,'status':'paid','payments':[pay],'shipping':{'id':7}}
        upsert_snapshot(db,'123','orders',str(id),'/orders/'+str(id),o)
    upsert_snapshot(db,'123','shipment_costs','7','/shipments/7/costs',{'senders':[{'user_id':123,'cost':15}]});db.commit()

def test_finance_deduplicates_pack_payment_and_shipping(connected):
    with SessionLocal() as db:
        seed_orders(db)
        view=finance_view(db,'123');r=view['results'][0]
        assert r['gross']=='200.00' and r['realized_fee']=='26.00' and r['seller_shipping']=='15.00'
        assert r['subtotal_before_refunds']=='159.00' and r['state']=='matched_components'
        assert view['bank_settlement_verified'] is False

def test_finance_missing_fee_is_unknown_not_zero(connected):
    with SessionLocal() as db:
        seed_orders(db,missing_fee=True);r=finance_view(db,'123')['results'][0]
        assert r['realized_fee'] is None and r['subtotal_before_refunds'] is None and r['state']=='needs_review'

def test_shared_shipment_across_packs_requires_allocation(connected):
    with SessionLocal() as db:
        seed_orders(db,grouped=False);rows=finance_view(db,'123')['results']
        assert len(rows)==2 and all(r['seller_shipping'] is None and r['subtotal_before_refunds'] is None for r in rows)

def test_mixed_currency_pack_is_not_summed_or_labeled_brl(connected):
    with SessionLocal() as db:
        seed_orders(db,mixed_currency=True);v=finance_view(db,'123');r=v['results'][0]
        assert r['currency'] is None and r['gross'] is None and r['subtotal_before_refunds'] is None
        assert v['totals']['UNKNOWN']['gross'] is None


def test_null_shipping_and_payments_do_not_break_financial_view(connected):
    with SessionLocal() as db:
        upsert_snapshot(db,'123','orders','1','/orders/1',{'id':1,'seller':{'id':123},'currency_id':'BRL','total_amount':100,'status':'paid','shipping':None,'payments':None});db.commit()
        r=finance_view(db,'123')['results'][0]
        assert r['state']=='needs_review' and r['seller_shipping'] is None and r['realized_fee'] is None


def test_billing_reconciliation_deduplicates_details_and_detects_divergence(connected):
    with SessionLocal() as db:
        db.add(DataJob(seller_id='123',status='completed',progress={'billing':{'status':'success'}}));db.commit()
        summary={'bill_includes':{'charges':[{'type':'CV','amount':30}],'bonuses':[]},'errors':[]}
        detail={'results':[{'charge_info':{'detail_id':1,'detail_amount':10,'detail_type':'CHARGE','detail_sub_type':'CV'}},{'charge_info':{'detail_id':2,'detail_amount':20,'detail_type':'CHARGE','detail_sub_type':'CV'}}]}
        upsert_snapshot(db,'123','billing','summary:ML:BILL:2026-11-01','/billing/summary',summary)
        upsert_snapshot(db,'123','billing','details:ML:BILL:2026-11-01:0','/billing/details',detail)
        upsert_snapshot(db,'123','billing','details:ML:BILL:2026-11-01:2','/billing/details',detail);db.commit()
        row=billing_view(db,'123')[0]
        assert row['state']=='matched' and row['components'][0]['observed']=='30.00'
        summary['bill_includes']['charges'][0]['amount']=35
        upsert_snapshot(db,'123','billing','summary:ML:BILL:2026-11-01','/billing/summary',summary);db.commit()
        row=billing_view(db,'123')[0]
        assert row['state']=='divergent' and row['components'][0]['difference']=='-5.00'

def test_billing_partial_sync_cannot_report_matched(connected):
    with SessionLocal() as db:
        db.add(DataJob(seller_id='123',status='completed_with_gaps',progress={'billing':{'status':'partial'}}));db.commit()
        upsert_snapshot(db,'123','billing','summary:ML:BILL:2026-11-01','/billing/summary',{'bill_includes':{'charges':[{'type':'CV','amount':10}]}})
        upsert_snapshot(db,'123','billing','details:ML:BILL:2026-11-01:0','/billing/details',{'results':[]});db.commit()
        row=billing_view(db,'123')[0]
        assert row['state']=='incomplete' and row['components'][0]['observed'] is None


def test_advertising_view_uses_daily_series_once_and_calculates_ratios(connected):
    with SessionLocal() as db:
        upsert_snapshot(db,'123','ads','campaign10','/advertising/MLB/advertisers/9/product_ads/campaigns/search',{'id':10,'name':'Campanha teste','currency_id':'BRL'})
        upsert_snapshot(db,'123','ads_metrics','daily10','/advertising/MLB/product_ads/campaigns/10',[{'date':'2026-11-01','clicks':2,'prints':100,'cost':5,'total_amount':50},{'date':'2026-11-02','clicks':3,'prints':200,'cost':5,'total_amount':50}]);db.commit()
        r=advertising_view(db,'123')[0]
        assert r['clicks']=='5.00' and r['prints']=='300.00' and r['cost']=='10.00'
        assert r['roas']=='10.00' and r['acos']=='10.00' and r['currency']=='BRL'

def test_zero_or_missing_ad_metrics_do_not_invent_ratios(connected):
    with SessionLocal() as db:
        upsert_snapshot(db,'123','ads_metrics','daily10','/advertising/MLB/product_ads/campaigns/10',[{'date':'2026-11-01','clicks':0,'prints':0,'cost':0,'total_amount':0}]);db.commit()
        r=advertising_view(db,'123')[0]
        assert r['roas'] is None and r['acos'] is None


def fee_order(id=1,fee=32.50,quantity=1,payment_fee=0,pack=None):
    # Synthetic IDs; reproduces split-payment fee representation, without buyer data.
    return {'id':id,'seller':{'id':123},'status':'paid','currency_id':'BRL','total_amount':250.0*quantity,'pack_id':pack,
        'order_items':[{'quantity':quantity,'unit_price':250.0,'sale_fee':fee,'currency_id':'BRL'}],
        'payments':[{'id':id*10,'status':'approved','transaction_amount':90.0*quantity,'marketplace_fee':payment_fee,'currency_id':'BRL'},
                    {'id':id*10+1,'status':'approved','transaction_amount':160.0*quantity,'marketplace_fee':0,'currency_id':'BRL'}],
        'shipping':{'id':7}}

def save_fee_order(db,order):
    upsert_snapshot(db,'123','orders',str(order['id']),'/orders/'+str(order['id']),order)
    upsert_snapshot(db,'123','shipment_costs','7','/shipments/7/costs',{'senders':[{'user_id':123,'cost':17.50}]});db.commit()

def test_finance_item_fee_overrides_zero_split_payment_commission(connected):
    with SessionLocal() as db:
        save_fee_order(db,fee_order());r=finance_view(db,'123')['results'][0]
        assert r['realized_fee']=='32.50' and r['seller_shipping']=='17.50'
        assert r['subtotal_before_refunds']=='200.00' and r['state']=='matched_components'
        assert r['fee_source']=='order_items.sale_fee' and r['fee_sources']=={'order_items':'32.50','payments':'0.00'}
        assert r['fee_notes']

def test_finance_item_fee_is_per_unit_not_per_payment(connected):
    with SessionLocal() as db:
        save_fee_order(db,fee_order(quantity=2));r=finance_view(db,'123')['results'][0]
        assert r['realized_fee']=='65.00' and r['subtotal_before_refunds']=='417.50'

def test_finance_agreeing_fee_sources_are_not_added(connected):
    with SessionLocal() as db:
        save_fee_order(db,fee_order(payment_fee=32.50));r=finance_view(db,'123')['results'][0]
        assert r['realized_fee']=='32.50' and r['subtotal_before_refunds']=='200.00'

def test_finance_positive_fee_disagreement_requires_review(connected):
    with SessionLocal() as db:
        save_fee_order(db,fee_order(payment_fee=20));r=finance_view(db,'123')['results'][0]
        assert r['state']=='needs_review' and r['subtotal_before_refunds'] is None
        assert any('diverge' in issue for issue in r['issues'])

def test_finance_missing_item_fee_with_zero_payment_is_not_free_sale(connected):
    with SessionLocal() as db:
        save_fee_order(db,fee_order(fee=None));r=finance_view(db,'123')['results'][0]
        assert r['realized_fee'] is None and r['subtotal_before_refunds'] is None
        assert r['state']=='needs_review'

def test_finance_confirmed_zero_item_fee_remains_valid(connected):
    with SessionLocal() as db:
        save_fee_order(db,fee_order(fee=0));r=finance_view(db,'123')['results'][0]
        assert r['realized_fee']=='0.00' and r['subtotal_before_refunds']=='232.50'

def test_finance_shared_pack_fee_is_counted_once_for_each_item(connected):
    with SessionLocal() as db:
        first=fee_order(id=1,pack=9);second=fee_order(id=2,pack=9)
        shared=[{'id':100,'status':'approved','transaction_amount':500.0,'marketplace_fee':0,'currency_id':'BRL'}]
        first['payments']=shared;second['payments']=shared
        save_fee_order(db,first);save_fee_order(db,second);r=finance_view(db,'123')['results'][0]
        assert r['realized_fee']=='65.00' and r['seller_shipping']=='17.50' and r['subtotal_before_refunds']=='417.50'

def test_finance_wrong_item_currency_or_invalid_quantity_requires_review(connected):
    with SessionLocal() as db:
        order=fee_order();order['order_items'][0]['currency_id']='USD'
        save_fee_order(db,order);assert finance_view(db,'123')['results'][0]['subtotal_before_refunds'] is None
        order=fee_order(quantity=1);order['order_items'][0]['quantity']=0.5
        save_fee_order(db,order);assert finance_view(db,'123')['results'][0]['subtotal_before_refunds'] is None
