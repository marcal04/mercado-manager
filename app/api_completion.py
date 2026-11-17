"""Deeper official API reads and an honest view of financial reconciliation coverage."""
import json
import re
from datetime import timedelta,timezone
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from html.parser import HTMLParser
from fastapi import APIRouter,Depends
from sqlalchemy.orm import Session
from .db import get_db
from .models import now
from .data_hub import Snapshot,ProviderError
from .marketplace_orders import latest
from .security import decrypt

router=APIRouter(prefix="/api/v1/api-details")
PADS_METRICS='clicks,prints,ctr,cost,cpc,acos,roas,cvr,direct_units_quantity,indirect_units_quantity,units_quantity,direct_amount,indirect_amount,total_amount'
class PlainText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.suppress=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.suppress+=1
        if tag in ('p','br','li','div'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.suppress=max(0,self.suppress-1)
    def handle_data(self,data):
        if not self.suppress:self.parts.append(data)
def plain(value):
    if isinstance(value,dict):return plain(value.get('content') or value.get('plain_text') or value.get('text') or '')
    if isinstance(value,list):return '\n'.join(plain(x) for x in value)
    parser=PlainText();parser.feed(str(value or ''))
    return re.sub(r'\n{3,}','\n\n',''.join(parser.parts)).strip()

def cached(db,seller,module,key):
    row=db.query(Snapshot).filter_by(seller_id=seller,module=module,external_id=str(key)).first()
    return json.loads(decrypt(row.payload_encrypted)) if row else None

def entries(db,seller,module):
    return [(r,json.loads(decrypt(r.payload_encrypted))) for r in db.query(Snapshot).filter_by(seller_id=seller,module=module)]

async def collect_descriptions(hub):
    for item in hub.items:
        id=item['id'];path=f'/items/{id}/description';listing=None
        try:listing=await hub.get(path)
        except ProviderError as e:
            if e.code!=404:hub.errors.append({'source':path,'http':e.code,'issue':'provider'});continue
        text=plain(listing)
        source='listing';catalog_id=item.get('catalog_product_id')
        if not text and catalog_id:
            catalog=cached(hub.db,hub.seller,'categories','catalog:'+catalog_id)
            if catalog is None:
                try:catalog=await hub.get('/products/'+catalog_id)
                except ProviderError as e:hub.errors.append({'source':'/products/'+catalog_id,'http':e.code,'issue':'catalog_description'});continue
            text=plain(catalog.get('short_description'));source='catalog';path='/products/'+catalog_id
        # Empty is a known absence, not a failed integration. Never manufacture a description.
        hub.save(id,path,{'item_id':id,'title':item.get('title'),'plain_text':text,'origin':source if text else 'absent',
            'catalog_product_id':catalog_id,'listing_description_present':bool(plain(listing)),
            'notice':'Texto oficial do catálogo; não é uma descrição personalizada do vendedor.' if source=='catalog' and text else 'Texto do anúncio.' if text else 'A API não forneceu texto para este produto.'})

async def collect_claim_details(hub):
    for claim in hub.claims:
        id=str(claim['id']);roles=claim.get('_integration_account_roles') or [p.get('role') for p in claim.get('players',[]) if str(p.get('user_id'))==hub.seller]
        path='/post-purchase/v1/claims/'+id
        try:
            detail=await hub.get(path)
        except ProviderError as e:
            hub.errors.append({'source':path,'http':e.code,'issue':'claim_detail_restricted' if e.code in (401,403) else 'claim_detail'})
            hub.save('access:'+id,path,{'claim_id':id,'account_roles':roles,'state':'restricted' if e.code in (401,403) else 'unavailable','http':e.code,
                'note':'Resumo disponível; a API negou os detalhes. O papel da conta nesta ocorrência não equivale a uma reclamação de venda.'})
            continue
        players=detail.get('players',claim.get('players',[]))
        if players and not any(str(p.get('user_id'))==hub.seller for p in players):raise ValueError('Foreign claim detail')
        hub.save(id,path,{**detail,'_integration_account_roles':roles})
        hub.save('access:'+id,path,{'claim_id':id,'account_roles':roles,'state':'available','note':'Detalhes retornados pela API.'})
        for suffix in ('detail','status-history','expected_resolutions'):
            await hub.capture(path+'/'+suffix,id+':'+suffix)

async def collect_ads_metrics(hub):
    end=(now().astimezone(timezone(timedelta(hours=-3)))-timedelta(days=1)).date();start=end-timedelta(days=29)
    dates={'date_from':start.isoformat(),'date_to':end.isoformat()}
    for typ,segment in [('PADS','product_ads'),('BADS','brand_ads'),('DISPLAY','display')]:
        advertisers=cached(hub.db,hub.seller,'ads',typ)
        if not advertisers:continue
        for a in advertisers.get('advertisers',[]):
            aid=str(a['advertiser_id']);site=a.get('site_id','MLB')
            if typ=='PADS':
                base=f'/advertising/{site}/advertisers/{aid}/product_ads'
                campaigns=await hub.pages(base+'/campaigns/search',{**dates,'metrics':PADS_METRICS,'aggregation_type':'DAILY'},headers={'api-version':'2'})
                groups=await hub.pages(base+'/ad_groups/search',{**dates,'metrics':PADS_METRICS.upper(),'metrics_summary':'true'},headers={'api-version':'2'})
                for campaign in campaigns:
                    cid=str(campaign.get('id',campaign.get('campaign_id','')))
                    if cid:await hub.capture(f'/advertising/{site}/product_ads/campaigns/{cid}',f'PADS:{aid}:campaign:{cid}:daily',{**dates,'metrics':PADS_METRICS,'aggregation_type':'DAILY'},{'api-version':'2'})
                for group in groups:
                    gid=group.get('ad_group_id',group.get('id'))
                    if gid:
                        await hub.pages(f'/advertising/{site}/product_ads/ad_groups/{gid}/ads',{**dates,'metrics':PADS_METRICS},headers={'api-version':'2'})
                        await hub.capture(f'/advertising/{site}/product_ads/ad_groups/{gid}',f'PADS:{aid}:group:{gid}',{**dates,'metrics':PADS_METRICS.upper()},{'api-version':'2'})
            else:
                path=f'/advertising/advertisers/{aid}/{segment}/campaigns'
                campaigns=await hub.pages(path,{},keys=('results','campaigns'),headers={'Api-Version':'1'})
                for c in campaigns:
                    cid=c.get('id',c.get('campaign_id'))
                    if cid:
                        await hub.capture(path+f'/{cid}/metrics',f'{typ}:{aid}:campaign:{cid}:metrics',dates,{'Api-Version':'1'})
                        if typ=='BADS':await hub.capture(path+f'/{cid}/keywords/metrics',f'{typ}:{aid}:campaign:{cid}:keywords',dates,{'Api-Version':'1'})

async def collect_financial(hub):
    for order in hub.orders:
        id=str(order['id']);await hub.capture('/orders/'+id+'/discounts','discounts:'+id)
    periods=set()
    for row,payload in entries(hub.db,hub.seller,'billing'):
        if row.source.endswith('/monthly/periods') and row.external_id.startswith('ML:'):
            periods.update(p['key'] for p in payload.get('results',[]) if p.get('key'))
    for key in sorted(periods):
        # This resource reports payments of billing documents, not seller wallet settlements.
        offset=0
        for page in range(100):
            path=f'/billing/integration/periods/key/{key}/group/ML/payment/details'
            identity=f'billing-payments:{key}:{offset}'
            previous=cached(hub.db,hub.seller,'financial',identity)
            row=hub.db.query(Snapshot).filter_by(seller_id=hub.seller,module='financial',external_id=identity).first()
            stamp=row.fetched_at.replace(tzinfo=timezone.utc) if row and row.fetched_at.tzinfo is None else row.fetched_at if row else None
            if stamp and now()-stamp<timedelta(hours=24):hub.identifiers.add(identity);payload=previous
            else:payload=await hub.capture(path,identity,{'offset':offset,'limit':100})
            if payload is None:break
            results=payload.get('results',payload.get('payments',[])) if isinstance(payload,dict) else payload
            if not isinstance(results,list):hub.partial=True;break
            offset+=len(results)
            total=payload.get('total',payload.get('paging',{}).get('total')) if isinstance(payload,dict) else None
            if not results or (total is not None and offset>=int(total)) or (total is None and len(results)<100):break
        else:hub.partial=True

def amount(value):
    try:
        n=Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation,ValueError,TypeError):return None

def fmt(n):return str(n.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)) if n is not None else None

def realized_sale_fees(orders,approved):
    """Two alternative fee representations, never additive.

    Some split-payment orders report marketplace_fee=0 on every payment while
    sale_fee is populated on each order item. The item amount is per unit.
    """
    payment_values=[amount(p.get('marketplace_fee')) for p in approved]
    payment_total=sum(payment_values,Decimal(0)) if approved and all(v is not None and v>=0 for v in payment_values) else None
    item_values=[];complete=bool(orders)
    for order in orders:
        items=order.get('order_items')
        if not isinstance(items,list) or not items:complete=False;continue
        for item in items:
            if not isinstance(item,dict):complete=False;continue
            fee=amount(item.get('sale_fee'));quantity=amount(item.get('quantity'))
            if fee is None or fee<0 or quantity is None or quantity<=0 or quantity!=quantity.to_integral_value() or item.get('currency_id') not in (None,order.get('currency_id')):
                complete=False;continue
            item_values.append(fee*quantity)
    item_total=sum(item_values,Decimal(0)) if complete else None
    issues=[];notes=[];source=None;fee=None
    if item_total is not None:
        fee=item_total;source='order_items.sale_fee'
        if item_total==0 and payment_total is not None and payment_total>0:
            fee=payment_total;source='payments.marketplace_fee'
            notes.append('Tarifa obtida nos pagamentos; a tarifa por item veio zerada. As duas fontes não foram somadas.')
        elif item_total>0 and payment_total==0:
            notes.append('Os pagamentos informaram comissão zero; foi usada a tarifa por unidade dos itens da venda, multiplicada pela quantidade.')
        elif payment_total is not None and payment_total>0 and abs(item_total-payment_total)>Decimal('0.01'):
            issues.append('Tarifa dos itens diverge da comissão dos pagamentos; confira as cobranças antes de concluir o líquido.')
    elif payment_total is not None and payment_total>0:
        fee=payment_total;source='payments.marketplace_fee'
    elif payment_total==0:
        issues.append('Os pagamentos informam comissão zero e a tarifa por item não pôde ser conferida; valor pendente.')
    else:
        issues.append('Tarifa realizada indisponível nos itens e pagamentos; cotação não é usada no lugar.')
    return fee,source,item_total,payment_total,issues,notes


def finance_view(db,seller):
    raw={r.external_id:p for r,p in entries(db,seller,'orders')};shipment_costs={r.external_id:p for r,p in entries(db,seller,'shipment_costs')}
    groups={};shipment_groups={};payment_groups={}
    for id,o in raw.items():
        if str(o.get('seller',{}).get('id'))!=seller:continue
        group=str(o.get('pack_id') or id);g=groups.setdefault(group,{'orders':[],'shipments':set(),'payments':{},'issues':[]})
        g['orders'].append(o)
        if (o.get('shipping') or {}).get('id'):
            sid=str(o['shipping']['id']);g['shipments'].add(sid);shipment_groups.setdefault(sid,set()).add(group)
        for p in (o.get('payments') or []):
            if p.get('id') is None:g['issues'].append('Pagamento sem identificador; não é possível eliminar duplicidade.');continue
            pid=str(p['id']);payment_groups.setdefault(pid,set()).add(group)
            if pid in g['payments'] and g['payments'][pid]!=p:g['issues'].append('O mesmo pagamento trouxe dados divergentes nas ordens do pack.')
            g['payments'][pid]=p
    rows=[];totals={}
    for id,g in groups.items():
        orders=g['orders'];issues=g['issues'];currencies={o.get('currency_id') for o in orders};currency=next(iter(currencies)) if len(currencies)==1 and None not in currencies else None
        gross_values=[amount(o.get('total_amount')) for o in orders];gross=sum(gross_values,Decimal(0)) if all(v is not None for v in gross_values) else None
        approved=[p for p in g['payments'].values() if p.get('status')=='approved']
        fees,fee_source,item_fees,payment_fees,fee_issues,fee_notes=realized_sale_fees(orders,approved)
        issues.extend(fee_issues)
        paid_values=[amount(p.get('transaction_amount')) for p in approved]
        paid=sum(paid_values,Decimal(0)) if approved and all(x is not None for x in paid_values) else None
        if any(len(payment_groups.get(pid,set()))>1 for pid in g['payments']):fees=paid=None;issues.append('Pagamento compartilhado entre packs; exige rateio antes de calcular.')
        if not currency:
            gross=fees=paid=None;issues.append('Moeda ausente ou divergente entre ordens.')
        if any(p.get('currency_id') not in (None,currency) for p in approved):fees=paid=None;issues.append('Moeda do pagamento difere da ordem.')
        shipping=Decimal(0);shipping_known=bool(g['shipments'])
        for sid in g['shipments']:
            costs=shipment_costs.get(sid)
            if len(shipment_groups[sid])>1:shipping_known=False;issues.append('Envio compartilhado entre packs; custo não é duplicado.');continue
            senders=[s for s in ((costs or {}).get('senders') or []) if str(s.get('user_id'))==seller]
            values=[amount(s.get('cost')) for s in senders]
            if not senders or any(v is None for v in values):shipping_known=False
            else:shipping+=sum(values,Decimal(0))
        if not currency:shipping_known=False
        if not shipping_known:shipping=None;issues.append('Custo de envio do vendedor indisponível ou sem envio vinculado.')
        if any(o.get('status')!='paid' for o in orders):issues.append('Pack contém ordem sem status pago ou com devolução/cancelamento.')
        if gross is None or paid is None or abs(gross-paid)>Decimal('0.01'):issues.append('Valor das ordens e pagamentos aprovados não coincide ou não pôde ser conferido.')
        if any((amount(p.get('transaction_amount_refunded')) or Decimal(0))>0 or p.get('status') in ('refunded','charged_back','in_mediation') for p in g['payments'].values()):issues.append('Há estorno, contestação ou mediação no pagamento; revise o fluxo financeiro.')
        all_clear=not issues
        subtotal=gross-fees-shipping if all_clear else None
        rows.append({'group_id':id,'order_ids':[str(o['id']) for o in orders],'currency':currency,'gross':fmt(gross),'approved_payment_amount':fmt(paid),'realized_fee':fmt(fees),'fee_source':fee_source if fees is not None else None,'fee_sources':{'order_items':fmt(item_fees),'payments':fmt(payment_fees)},'fee_notes':fee_notes,'seller_shipping':fmt(shipping),'subtotal_before_refunds':fmt(subtotal),'state':'matched_components' if all_clear else 'needs_review','issues':list(dict.fromkeys(issues)),
            'notice':'Conferência de componentes da API. Não comprova saldo recebido, repasse bancário ou lucro; custos de aquisição, impostos próprios, publicidade e estornos não foram abatidos.'})
        bucket=totals.setdefault(currency or 'UNKNOWN',{'gross':Decimal(0),'groups':0,'matched_groups':0,'subtotal':Decimal(0),'missing_gross':False})
        if gross is not None:bucket['gross']+=gross
        else:bucket['missing_gross']=True
        bucket['groups']+=1
        if all_clear:bucket['matched_groups']+=1;bucket['subtotal']+=subtotal
    return {'results':rows,'totals':{k:{'gross':None if v['missing_gross'] else fmt(v['gross']),'groups':v['groups'],'matched_groups':v['matched_groups'],'subtotal_matched_components':fmt(v['subtotal']) if v['matched_groups'] else None} for k,v in totals.items()},'scope':'imported_orders_api_components','bank_settlement_verified':False}

def billing_view(db,seller):
    from .data_hub import DataJob
    latest_job=db.query(DataJob).filter_by(seller_id=seller).order_by(DataJob.id.desc()).first()
    complete=bool(latest_job and latest_job.progress.get('billing',{}).get('status')=='success')
    snapshots=entries(db,seller,'billing');scopes={}
    for row,payload in snapshots:
        match=re.fullmatch(r'(summary|details):(ML|MP):(BILL|CREDIT_NOTE):(\d{4}-\d{2}-\d{2})(?::.*)?',row.external_id)
        if not match:continue
        kind,group,document,period=match.groups();scope=scopes.setdefault((group,document,period),{'summary':None,'details':[],'times':[]})
        scope['times'].append(row.fetched_at)
        if kind=='summary':scope['summary']=payload
        else:scope['details'].append(payload)
    result=[]
    def charge_rows(node):
        if isinstance(node,list):
            for value in node:yield from charge_rows(value)
        elif isinstance(node,dict):
            if isinstance(node.get('charge_info'),dict):yield node['charge_info']
            for key,value in node.items():
                if key!='charge_info' and isinstance(value,(dict,list)):yield from charge_rows(value)
    for (group,document,period),scope in sorted(scopes.items()):
        expected={};observed={};seen={};issues=[];currency=None
        if scope['summary'] is None:issues.append('Resumo indisponível.')
        else:
            includes=scope['summary'].get('bill_includes',{})
            for field,kind in [('charges','CHARGE'),('bonuses','BONUS')]:
                for r in includes.get(field) or []:
                    value=amount(r.get('amount'));subtype=r.get('type')
                    if value is None or not subtype:issues.append('Componente do resumo sem tipo ou valor.');continue
                    key=(kind,str(subtype));expected[key]=expected.get(key,Decimal(0))+value
            if scope['summary'].get('errors'):issues.append('O resumo contém pendências informadas pelo provedor.')
        for payload in scope['details']:
            if payload.get('errors'):issues.append('O detalhe contém pendências informadas pelo provedor.')
            for row in charge_rows(payload):
                detail_id=row.get('detail_id');value=amount(row.get('detail_amount'));subtype=row.get('detail_sub_type');kind=str(row.get('detail_type','')).upper()
                if detail_id is None or value is None or not subtype or kind not in ('CHARGE','BONUS'):
                    issues.append('Detalhe sem identificador, tipo ou valor; exige revisão.');continue
                if str(detail_id) in seen:
                    if seen[str(detail_id)]!=row:issues.append('Identificador de cobrança repetido com valores divergentes.')
                    continue
                seen[str(detail_id)]=row;key=(kind,str(subtype));observed[key]=observed.get(key,Decimal(0))+value
        if not scope['details']:issues.append('Detalhes do período indisponíveis.')
        if not complete:issues.append('A última consulta de faturamento não concluiu todos os recursos.')
        components=[]
        for key in sorted(set(expected)|set(observed)):
            target=expected.get(key);actual=observed.get(key,Decimal(0)) if complete and scope['details'] else None
            difference=actual-target if actual is not None and target is not None else None
            components.append({'kind':key[0],'type':key[1],'expected':fmt(target),'observed':fmt(actual),'difference':fmt(difference),
                'state':'incomplete' if issues or difference is None else 'matched' if abs(difference)<=Decimal('0.01') else 'divergent'})
        result.append({'group':group,'document_type':document,'period':period,'components':components,'issues':list(dict.fromkeys(issues)),
            'state':'incomplete' if issues or any(c['state']=='incomplete' for c in components) else 'divergent' if any(c['state']=='divergent' for c in components) else 'matched' if components else 'no_components',
            'latest_snapshot':max(scope['times']) if scope['times'] else None,'scope':'billing_documents_not_bank_settlement'})
    return result

def advertising_view(db,seller):
    metadata=entries(db,seller,'ads');rows=[]
    for row,payload in entries(db,seller,'ads_metrics'):
        match=re.fullmatch(r'/advertising/(ML[A-Z])/product_ads/campaigns/(\d+)',row.source)
        brand=re.fullmatch(r'/advertising/advertisers/(\d+)/(brand_ads|display)/campaigns/(\d+)/metrics',row.source)
        if not match and not brand:continue
        cid=match.group(2) if match else brand.group(3);typ='PADS' if match else 'BADS' if brand.group(2)=='brand_ads' else 'DISPLAY'
        campaign=next((p for r,p in metadata if isinstance(p,dict) and str(p.get('id'))==cid and '/campaigns' in r.source and (match or '/advertisers/'+brand.group(1)+'/' in r.source)),{})
        series=payload if isinstance(payload,list) else payload.get('metrics') if isinstance(payload.get('metrics'),list) else payload.get('results') if isinstance(payload.get('results'),list) else [payload.get('metrics',payload)]
        def total(field):
            values=[amount((p.get('metrics') or p).get(field)) for p in series if isinstance(p,dict)]
            return sum(values,Decimal(0)) if values and all(v is not None for v in values) else None
        clicks=total('clicks');prints=total('prints');cost=total('cost');revenue=total('total_amount')
        dates=sorted(p['date'] for p in series if isinstance(p,dict) and p.get('date'))
        currency=campaign.get('currency_id') or (payload.get('currency_id',payload.get('currency')) if isinstance(payload,dict) else None)
        if not currency and match and match.group(1)=='MLB':currency='BRL'
        rows.append({'campaign_id':cid,'name':campaign.get('name') or 'Campanha #'+cid,'product':typ,'currency':currency,'date_from':dates[0] if dates else None,'date_to':dates[-1] if dates else None,
            'clicks':fmt(clicks),'prints':fmt(prints),'cost':fmt(cost),'attributed_revenue':fmt(revenue),
            'roas':fmt(revenue/cost) if cost is not None and cost>0 and revenue is not None else None,
            'acos':fmt(cost/revenue*100) if revenue is not None and revenue>0 and cost is not None else None,'fetched_at':row.fetched_at,'source':row.source})
    return rows
@router.get('/advertising')
def advertising(db:Session=Depends(get_db)):
    c=latest(db)
    return advertising_view(db,c.seller_id) if c else []

@router.get('/descriptions')
def descriptions(db:Session=Depends(get_db)):
    c=latest(db)
    return [{'item_id':r.external_id,'source':r.source,'fetched_at':r.fetched_at,**p} for r,p in entries(db,c.seller_id,'descriptions')] if c else []
@router.get('/claims')
def claim_access(db:Session=Depends(get_db)):
    c=latest(db)
    if not c:return []
    records=dict((r.external_id,p) for r,p in entries(db,c.seller_id,'claims'));rows=[]
    for key,p in records.items():
        if key.startswith('access:') or ':' in key:continue
        roles=p.get('_integration_account_roles') or [x.get('role') for x in p.get('players',[]) if str(x.get('user_id'))==c.seller_id]
        if not roles:continue
        access=records.get('access:'+str(p.get('id',key)),{})
        rows.append({'id':str(p.get('id',key)),'type':p.get('type'),'status':p.get('status'),'account_roles':roles,'detail_state':access.get('state','not_collected'),'note':access.get('note','Resumo recebido; detalhes ainda não conferidos.')})
    return rows
@router.get('/finance')
def finance(db:Session=Depends(get_db)):
    c=latest(db)
    return {**finance_view(db,c.seller_id),'billing':billing_view(db,c.seller_id)} if c else {'results':[],'totals':{},'billing':[],'bank_settlement_verified':False}
