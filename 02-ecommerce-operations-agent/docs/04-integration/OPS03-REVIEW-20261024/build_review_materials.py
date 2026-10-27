"""Review-only material builder: no network, DB engine, business writes or source edits.

The archive read double bypasses persistence/identity, NOT the current dashboard
projection or domain calculations. It cannot prove HTTP auth, SQL or PostgreSQL.
Run with the 02 .venv and PYTHONDONTWRITEBYTECODE=1. Generated files are proposals.
"""
import sys
sys.dont_write_bytecode = True
import json
import hashlib
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = next(p for p in OUT.parents if p.name == '02-ecommerce-operations-agent')
sys.path.insert(0, str(ROOT / 'backend/src'))
from ops_agent.domain import calculate_metrics, validate_snapshot, DomainError
from ops_agent.service import Operations
from ops_agent.auth import Principal
from ops_agent.errors import AppError

def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()

def save(name, value):
    path = OUT / name
    if path.exists():
        raise RuntimeError('Preserve existing review artifact: ' + name)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

SCHEMA_REV = 'OPS-QUERY-v0.2-draft.1'
METRIC_REV = 'OPS-METRIC-v0.1-review.1'
COMMON = {'profile': 'operations_store', 'status': '02_review_not_jointly_approved',
          'business_schema_changed': False, 'production_sql_available': False}

def field(name, type_, nullable=False, **kw):
    return {'name': name, 'type': type_, 'nullable': nullable, **kw}

current = {
 'snapshot': [field('snapshot_id','string'),field('revision','integer>=1'),field('store_id','string'),
   field('source_kind','enum:synthetic|business_import|derived'),field('as_of','ISO8601_tz'),
   field('currency','const:CNY'),field('skus','array<Sku>'),field('orders','array<Order>'),
   field('refunds','array<Refund>')],
 'Sku': [field('sku_id','string'),field('title','string'),field('category','string'),
   field('base_price_minor','unsigned_minor_string>0'),field('cost_minor','unsigned_minor_string',True,capability='cost.read'),
   field('variable_fee_minor','unsigned_minor_string',True,capability='cost.read'),
   field('available_qty','integer>=0',True),field('price_version','integer>=1')],
 'Order': [field('order_id','string'),field('paid_at','ISO8601_tz',True),
   field('status','enum:paid|unpaid|cancelled'),field('channel','string; blank allowed'),
   field('paid_minor','unsigned_minor_string'),field('items','nonempty array<OrderItem>')],
 'OrderItem': [field('sku_id','string'),field('qty','integer>=1'),field('paid_minor','unsigned_minor_string')],
 'Refund': [field('refund_id','string'),field('order_id','string'),field('occurred_at','ISO8601_tz'),
   field('status','enum:succeeded|pending|failed'),field('amount_minor','unsigned_minor_string')],
}
sql = {
 'ops_order': {'status':'proposed_unimplemented','grain':'one normalized order in server-bound snapshot revision',
   'key':['order_id'],'fields':[field('order_id','TEXT'),field('status','TEXT'),field('paid_at','TIMESTAMPTZ',True),
   field('channel','TEXT',normalization='strip; empty=>unknown'),field('paid_minor','BIGINT'),field('currency','TEXT',derived='snapshot.currency')]},
 'ops_order_item': {'status':'proposed_unimplemented','grain':'one array item in a fixed order snapshot',
   'key':['order_id','line_no'],'fields':[field('order_id','TEXT'),field('line_no','BIGINT',derived='1-based array ordinal; not native order_item_id'),
   field('sku_id','TEXT'),field('qty','BIGINT'),field('paid_minor','BIGINT')]},
 'ops_refund': {'status':'proposed_unimplemented','grain':'one normalized refund event',
   'key':['refund_id'],'fields':[field('refund_id','TEXT'),field('order_id','TEXT'),field('occurred_at','TIMESTAMPTZ'),
   field('status','TEXT'),field('amount_minor','BIGINT')]},
 'ops_sku': {'status':'proposed_unimplemented','grain':'one sku at the fixed snapshot instant',
   'key':['sku_id'],'fields':[field('sku_id','TEXT'),field('title','TEXT'),field('category','TEXT'),
   field('base_price_minor','BIGINT'),field('price_version','BIGINT'),field('available_qty','BIGINT',True),
   field('as_of','TIMESTAMPTZ',derived='snapshot.as_of'),field('cost_minor','BIGINT',True,capability='cost.read'),
   field('variable_fee_minor','BIGINT',True,capability='cost.read')]},
}
public_sql = deepcopy(sql)
public_sql['ops_sku']['fields'] = [v for v in public_sql['ops_sku']['fields'] if not v.get('capability')]
schema_for_model = {'revision':SCHEMA_REV,'dialect':'postgresql','resources':public_sql,
    'scope':'store+snapshot_id+revision pinned by02; not user/model WHERE',
    'status':'proposed_unimplemented_not_training_frozen'}
schema_digest = sha(schema_for_model)
save('schema-and-fields.json', {**COMMON,'document_revision':SCHEMA_REV,
 'evidence':'source inspection; SQL names below do NOT exist as views',
 'current_physical_archive':{'table':'ops_snapshots','key':['store_id','snapshot_id','revision'],
   'columns':['store_id','snapshot_id','revision','digest','body','created_at'],
   'model_may_query_raw_archive':False},
 'current_snapshot_fields':current,
 'optional_snapshot_fields':{'schema_version':'OPS-SNAPSHOT-v0.1|OPS-SNAPSHOT-v0.2',
   'execution_supported_rules':'array<string>; not authority to enable rules',
   'source_metadata':'producer/source_revision/cost_basis/capacity_basis',
   'provenance':'parents[{snapshot_id,revision,digest}]/transformation/inputs_version; v0.2 required'},
 'current_validation_notes':[
   'strict field shapes; full snapshot validation in domain.py, not JSON Schema alone',
   'each input integer or money <=9223372036854775807; booleans not integers; strings digits only',
   'deduplicate identical sku_id/order_id/refund_id; conflicting duplicates reject',
   'items have NO row id and NO uniqueness constraint on (order_id,sku_id)',
   'sum(items.paid_minor)==order.paid_minor; item.sku_id must exist',
   'paid status requires paid_at; current validator permits zero-value paid orders',
   'succeeded refund requires paid original order, occurred_at>=paid_at; cumulative succeeded amount<=paid_minor',
   'unknown enum rejects invalid_snapshot; pending/failed refunds do not affect metrics',
   'as_of is snapshot event-time watermark; no per-event ingestion timestamps',
   'available_qty may be null; no warehouse or historical stock series'],
 'proposed_sql_resources':sql,
 'proposed_relations':[{'from':'ops_order_item.order_id','to':'ops_order.order_id','cardinality':'N:1'},
   {'from':'ops_order_item.sku_id','to':'ops_sku.sku_id','cardinality':'N:1'},
   {'from':'ops_refund.order_id','to':'ops_order.order_id','cardinality':'N:1'}],
 'fanout_warning':'order_item N x refund M must not repeat order-level payment; aggregate independently or use approved semijoin',
 'excluded_resources':{'ops_payment':'no native payment event id/array',
   'ops_cost':'no historical effective intervals', 'ops_inventory':'only sku.available_qty at as_of; separate series unimplemented',
   'ops_sales_period':'no persistent model view', 'ops_traffic_period':'no visitor/traffic source',
   'ops_activity_actuals':'exists in02 archive but not approved SQL model projection'},
 'public_schema_example':schema_for_model,'public_schema_example_digest':schema_digest,
 'digest_algorithm':'sha256(UTF8(json.dumps(schema,ensure_ascii=False,sort_keys=True,separators=(comma,colon))))',
 'digest_warning':'draft schema artifact digest only; no deployed validator recognizes it; cost projection requires another digest',
 'pending':['sandbox DDL/loader','02 RLS or equally enforced scope projection','AST/function allowlist',
   'execution endpoint','approved immutable schema/metric versions','joint acceptance']})

definitions = [
 ('payment_minor','cash_period','sum original paid_minor for paid orders with start<=paid_at<end and paid_at<=visible','minor_string','0'),
 ('refund_minor','cash_period','sum succeeded amount_minor with start<=occurred_at<end and occurred_at<=visible','minor_string','0 unless selected-SKU cash refund allocation unknown'),
 ('net_paid_minor','cash_period','payment_minor-refund_minor; may be negative','signed_minor_string','null if cash refund unknown'),
 ('paid_order_count','payment_cohort','count distinct qualifying order_id','integer','0'),
 ('paid_units','payment_cohort','sum original item.qty for qualifying orders; no return subtraction','integer','0'),
 ('average_order_minor','payment_cohort','payment_minor/paid_order_count; ROUND_HALF_UP to integer minor','minor_string_or_null','null iff no qualifying orders'),
 ('cohort_refund_order_rate','payment_cohort','distinct cohort order_ids with any visible succeeded refund / cohort order count','ratio6_string_or_null','null if no orders or cohort allocation unknown'),
 ('cohort_refund_amount_rate','payment_cohort','visible succeeded refunds linked to cohort / cohort original paid_minor','ratio6_string_or_null','null if payment<=0 or cohort allocation unknown'),
]
metrics = [{'id':name,'current_response_field':'metrics.'+name,'family':family,'formula':formula,
            'output_type':typ,'zero_null':nil,'status':'implemented_fixed_tool_not_SQL_contract'}
           for name,family,formula,typ,nil in definitions]
params = [
 {'name':'snapshot_id','current_http':'optional; highest revision of id or default archive head','proposed':'required for query/fixture'},
 {'name':'revision','current_http':'not accepted as an explicit selector','proposed':'required integer>=1; exact immutable reference'},
 {'name':'snapshot_digest','current_http':'not exposed in dashboard snapshot','proposed':'required immutable archive digest'},
 {'name':'as_of','current_http':'no request parameter; service uses trusted clock','proposed':'required for reproducible query; compute visible=min(requested,snapshot.as_of,trusted_clock)'},
 {'name':'start_at','current_http':'optional; end-30d default','proposed':'RFC3339 aware timestamp; missing=>clarify'},
 {'name':'end_at','current_http':'optional; trusted clock default','proposed':'RFC3339 aware timestamp; start<end; right exclusive'},
 {'name':'sku_id','current_http':'optional; must belong to snapshot','proposed':'optional TEXT bind; no invented sku values'},
 {'name':'channel','current_http':'optional; must belong to snapshot after trim/unknown normalization','proposed':'optional TEXT bind'},
]
limits = {'status':'proposed_unimplemented_not_SLA','sql_utf8_bytes':8192,'ast_nodes':512,
 'joins':3,'nonrecursive_ctes':2,'max_detail_rows':50,'max_response_bytes':262144,
 'statement_timeout_ms':2000,'lock_timeout_ms':500,'inflight_per_actor':1,'inflight_per_instance':2,
 'max_window_days':366,'automatic_sql_retries':0,'transient_model_transport_max_retries':1,
 'explain_cost_budget':None,'cost_budget_not_configured':'fail_closed',
 'model_deadline_and_token_budget':'03 service agreement pending; no inherited01 model limits'}
save('metrics-and-parameters.json',{**COMMON,'catalog_revision':METRIC_REV,'formula_source':'backend/src/ops_agent/domain.py:251-308; service.py:145-234',
 'deployed_dashboard_metric_revision':None,'plan_lineage_only':{'metric_revision':'OPS-METRIC-v0.1','calculation_revision':'OPS-CALC-v0.1'},
 'metrics':metrics,'visible_semantics':'min(domain as_of,snapshot.as_of); dashboard passes trusted clock',
 'timezone':'Asia/Shanghai','window':'[start_at,end_at); event visible cutoff inclusive',
 'ratio_rounding':'ROUND_HALF_UP to 0.000001','amount_rounding':'ROUND_HALF_UP to1minor; input minor is exact integer string',
 'aggregation_precision':'80 significant Decimal digits; SUM can exceed per-field BIGINT',
 'channels':{'fields':['channel','payment_minor','paid_order_count'],'status':'implemented',
   'additive_when':'same snapshot/revision/window/visible/filters; complete mutually exclusive channel partition'},
 'history':{'status':'implemented sparse Shanghai day buckets','fields':['date','payment_minor','refund_minor','refund_missing?'],
   'no_event_date':'row absent, not automatically filled0'},
 'non_additive':['cross-SKU distinct order count','average_order_minor','cohort refund rates','stock across time'],
 'refund_unknown':'positive succeeded mixed-SKU order refund lacks line allocation; cash and cohort unknown determined independently',
 'zero_refund_event':'current succeeded zero-amount event counts in refunded-order numerator; policy confirmation pending',
 'actual_profit':'always null for authorized dashboard; omitted without cost.read; no registered historical profit SQL metric',
 'cost_authorization':'online backend Principal; demo roles not shared authorization contract',
 'parameters':params,'proposed_limits':limits,
 'pending_projection_metadata':['schema_revision','schema_digest','metric_revision','calculation_revision',
   'snapshot.digest','requested_as_of','visible_at','coverage','unknown_reasons'],
 'unsupported':['visitor conversion','historical warehouse stock','historical cost-effective joins','created_at order metrics',
   'unapproved profit formulas','split payment events','refund quantities','weekly/monthly group API'],
 'current_api':'GET /api/ops/v1/dashboard; {data:...}; errors {error:{code,message},request_id}'})

def t(text): return datetime.fromisoformat(text)
START='2026-11-01T00:00:00+08:00'
END='2026-12-01T00:00:00+08:00'
CLOCK='2026-12-10T00:00:00+08:00'
def item(sku, qty, money): return {'sku_id':sku,'qty':qty,'paid_minor':money}
def order(key, when, money, items=None, channel='web', status='paid'):
    return {'order_id':key,'paid_at':when,'status':status,'channel':channel,'paid_minor':money,
            'items':items or [item('A',1,money)]}
def refund(key, oid, when, money, status='succeeded'):
    return {'refund_id':key,'order_id':oid,'occurred_at':when,'status':status,'amount_minor':money}
base = {'snapshot_id':'ops03_review_synthetic','revision':1,'store_id':'fixture_store_A','source_kind':'synthetic',
 'as_of':CLOCK,'currency':'CNY','skus':[{'sku_id':s,'title':'合成商品'+s,'category':'合成分类',
 'base_price_minor':'10000','cost_minor':'6000','variable_fee_minor':'500','available_qty':120,'price_version':1} for s in ('A','B')],
 'orders':[order('A','2026-11-01T00:05:00+08:00','10000'),
   order('B','2026-11-15T10:01:00+08:00','30000',[item('A',1,'10000'),item('B',2,'20000')]),
   order('C',None,'0',status='cancelled'),
   order('D','2026-11-25T09:01:00+08:00','20000',[item('B',2,'20000')],channel='')],
 'refunds':[refund('R-B','B','2026-11-20T10:00:00+08:00','10000'),
   refund('R-D','D','2026-12-05T10:00:00+08:00','20000')]}
cases=[]
def case(key, label, source=None, expected=None, mode='dashboard_read_double', **kwargs):
    cases.append({'case_id':key,'label':label,'profile':'operations_store','source_kind':'synthetic',
      'mode':mode,'snapshot':deepcopy(base if source is None else source),
      'parameters':{'start_at':START,'end_at':END,'as_of':CLOCK, **kwargs.pop('parameters',{})},
      'expected':expected or {}, **kwargs})
normal={'payment_minor':'60000','refund_minor':'10000','net_paid_minor':'50000','paid_order_count':3,
 'paid_units':6,'average_order_minor':'20000','cohort_refund_order_rate':'0.666667','cohort_refund_amount_rate':'0.500000'}
case('F01','正常与跨期退款：03四订单示例的02 oracle',expected=normal)
early={**normal,'cohort_refund_order_rate':'0.333333','cohort_refund_amount_rate':'0.166667'}
case('F02','同归档早as_of事件重算，不等于历史已知状态重建',expected=early,mode='domain',parameters={'as_of':'2026-11-30T00:00:00+08:00'})
empty=deepcopy(base); empty.update(orders=[],refunds=[])
case('F03','完整空事件快照：零与无分母',empty,{'payment_minor':'0','refund_minor':'0','net_paid_minor':'0','paid_order_count':0,
 'paid_units':0,'average_order_minor':None,'cohort_refund_order_rate':None,'cohort_refund_amount_rate':None},expected_history_empty=True)
negative=deepcopy(base);negative['orders']=[order('old','2026-10-31T10:00:00+08:00','5000')]
negative['refunds']=[refund('old-R','old','2026-11-05T10:00:00+08:00','5000')]
case('F04','窗外支付本窗退款：负现金期净额',negative,{'payment_minor':'0','refund_minor':'5000','net_paid_minor':'-5000','paid_order_count':0})
dup=deepcopy(base);dup['orders'].append(deepcopy(dup['orders'][1]));dup['refunds'].append(deepcopy(dup['refunds'][0]))
case('F05','重复订单/退款身份去重；不按原始数组重复汇总',dup,normal)
conflict=deepcopy(base);bad=deepcopy(conflict['orders'][0]);bad['channel']='conflict';conflict['orders'].append(bad)
case('F06','冲突重复主键整批拒绝',conflict,expected_error='invalid_snapshot')
missing=deepcopy(base);missing['skus'][0].update(cost_minor=None,variable_fee_minor=None)
case('F07','成本缺失不抹去收入；实际利润未知',missing,normal,expected_profit_null=True)
case('F08','单SKU原生支付已知、混合SKU退款无分摊',expected={'payment_minor':'20000','refund_minor':None,
 'net_paid_minor':None,'paid_order_count':2,'paid_units':2,'cohort_refund_order_rate':None,'cohort_refund_amount_rate':None},
 parameters={'sku_id':'A'},expected_unknown_refund_day='2026-11-20')
case('F09','cohort退款在现金窗后：现金不变、cohort未知',expected={'payment_minor':'20000','refund_minor':'0',
 'net_paid_minor':'20000','cohort_refund_order_rate':None,'cohort_refund_amount_rate':None},
 parameters={'sku_id':'A','end_at':'2026-11-16T00:00:00+08:00'})
case('F10','成本无权限：字段省略而非自报权限',expected=normal,capabilities=['metrics.read'],expected_cost_omitted=True)
case('F11','无metrics能力后端拒绝',expected_error='forbidden',capabilities=[])
zero=deepcopy(base);zero.update(orders=[order('zero','2026-11-03T10:00:00+08:00','0')],refunds=[refund('zero-R','zero','2026-11-04T10:00:00+08:00','0')])
case('F12','零元支付＋成功零额退款：不能过滤paid_minor>0',zero,{'payment_minor':'0','refund_minor':'0','paid_order_count':1,
 'paid_units':1,'average_order_minor':'0','cohort_refund_order_rate':'1.000000','cohort_refund_amount_rate':None})
half=deepcopy(empty);half['orders']=[order('half1','2026-11-03T10:00:00+08:00','10000'),order('half2','2026-11-03T10:01:00+08:00','10001')]
case('F13','客单价半分按约定HALF_UP',half,{'payment_minor':'20001','paid_order_count':2,'average_order_minor':'10001'})
case('F14','不存在渠道不是完整空结果',expected_error='invalid_filter',parameters={'channel':'not_present'})
edge=deepcopy(empty);edge['as_of']=END;edge['orders']=[order('left',START,'10000'),order('right',END,'20000')]
case('F15','左边界含/右边界不含',edge,{'payment_minor':'10000','paid_order_count':1},mode='domain')
fanout=deepcopy(empty);fanout['orders']=[order('multi','2026-11-03T10:00:00+08:00','30000',[item('A',1,'10000'),item('B',2,'20000')])]
fanout['refunds']=[refund('m1','multi','2026-11-04T10:00:00+08:00','4000'),refund('m2','multi','2026-11-05T10:00:00+08:00','6000')]
case('F16','一单两行两退款：SQL扇出反例oracle',fanout,{'payment_minor':'30000','refund_minor':'10000','net_paid_minor':'20000',
 'paid_order_count':1,'paid_units':3,'cohort_refund_order_rate':'1.000000','cohort_refund_amount_rate':'0.333333'})
unknown=deepcopy(base);unknown['orders'][0]['status']='completed'
case('F17','02不直接接01交易状态枚举',unknown,expected_error='invalid_snapshot')
sameline=deepcopy(empty);sameline['orders']=[order('same-sku','2026-11-03T10:00:00+08:00','30000',[item('A',1,'10000'),item('A',2,'20000')])]
case('F18','同订单同SKU可有两行：行键不能用order_id+sku_id',sameline,{'payment_minor':'30000','paid_order_count':1,'paid_units':3})
zeroevent=deepcopy(empty);zeroevent['orders']=[order('paid','2026-11-03T10:00:00+08:00','10000')]
zeroevent['refunds']=[refund('zr','paid','2026-11-04T10:00:00+08:00','0')]
case('F19','成功零额事件计入订单退款率：待业务确认',zeroevent,{'cohort_refund_order_rate':'1.000000','cohort_refund_amount_rate':'0.000000'})
stock=deepcopy(base);stock['skus'][0]['available_qty']=None
case('F20','库存未知保持null',stock,normal,expected_stock_null=True)

class NoDatabase:
    @contextmanager
    def transaction(self, lock_scope=None):
        assert lock_scope is None
        yield None
class ArchiveReadDouble(Operations):
    def _snapshot(self, _connection, store, identity=None):
        assert store == self.fixture['store_id']
        assert identity is None or identity == self.fixture['snapshot_id']
        return deepcopy(self.fixture)

observed=[]
for entry in cases:
    raw=deepcopy(entry['snapshot']); before=sha(raw)
    params=entry['parameters']; result=None
    try:
        normalized=validate_snapshot(raw)
        entry['normalized_snapshot_digest']=sha(normalized)
        if entry['mode']=='domain':
            metric=calculate_metrics(normalized,t(params['start_at']),t(params['end_at']),t(params['as_of']))
        else:
            principal=Principal('synthetic-reviewer','fixture_store_A',tuple(entry.get('capabilities',['metrics.read','cost.read'])),'unused',mode='test')
            operations=ArchiveReadDouble(NoDatabase(),lambda:t(params['as_of']),'test',None)
            operations.fixture=normalized
            result=operations.dashboard(principal,normalized['snapshot_id'],params['start_at'],params['end_at'],params.get('sku_id'),params.get('channel'))
            metric=result['metrics']
        assert 'expected_error' not in entry, entry['case_id']+' expected rejection'
        for key, expected in entry['expected'].items():
            assert metric[key]==expected,(entry['case_id'],key,metric[key],expected)
        if entry.get('expected_history_empty'): assert result['history']==[]
        if entry.get('expected_profit_null'): assert result['contribution_profit_minor'] is None and result['profit_missing_reason']
        if entry.get('expected_cost_omitted'):
            assert all('cost_minor' not in row and 'variable_fee_minor' not in row for row in result['skus'])
            assert all(k not in result for k in ('contribution_profit_minor','cost_coverage','profit_missing_reason'))
        if entry.get('expected_unknown_refund_day'):
            bucket=next(row for row in result['history'] if row['date']==entry['expected_unknown_refund_day'])
            assert bucket['refund_minor'] is None and bucket['refund_missing']
        if entry.get('expected_stock_null'): assert result['skus'][0]['available_qty'] is None
        assert sha(raw)==before,'Do not mutate fixture'
        observed.append({'case_id':entry['case_id'],'status':'passed','metrics':metric,
                         'response':result,'snapshot_digest':entry['normalized_snapshot_digest']})
    except (DomainError,AppError) as exc:
        assert exc.code==entry.get('expected_error'),(entry['case_id'],exc.code,entry.get('expected_error'))
        assert sha(raw)==before
        observed.append({'case_id':entry['case_id'],'status':'passed_expected_rejection','error_code':exc.code})

static_cases=[
 {'case_id':'S01','label':'跨店资源隔离/同Cookie撤权','status':'source_test_and_historical_R1_only_not_run_here',
  'test':'backend/tests/test_api.py:307 test_identity_uses_current_capabilities_and_fails_closed_on_old_contract',
  'conditions':'actor storeA; resource only storeB; do not send storeB as model parameter',
  'expected':'backend inaccessible resource404; no storeB rows/ids; withdrawn cost403; old identity503'},
 {'case_id':'S02','label':'SQL写/系统目录/缺WHERE/成本派生绕过','status':'proposed_unimplemented_execution_suite',
  'expected':'02 independent enforcement rejects; no business execution or credentials in03'},
 {'case_id':'S03','label':'未知Schema/metric版本、缺窗口与退款率歧义','status':'proposed_protocol_cases',
  'expected':'mismatch=>error; missing slots=>clarify; unsupported feature=>unsupported'},
]
save('fixtures.json',{**COMMON,'fixture_revision':'OPS03-FIXTURE-review.1','data_classification':'synthetic_only',
 'usage':'contract examples/candidate oracle; NOT jointly frozen gold or training approval',
 'runtime_limits':'no SQL/PG/HTTP/auth runtime proof; service archive read is replaced',
 'cases':cases,'static_or_pending_cases':static_cases})
save('fixture-results.json',{'captured_at':datetime.now(timezone.utc).isoformat(),
 'verification_level':'isolated current Python domain/dashboard with normalized synthetic JSON and archive read double',
 'network_calls':0,'database_engines_created':0,'business_writes':0,'model_calls':0,
 'does_not_prove':['HTTP','real identity/tenant isolation','SQL validation/execution','PostgreSQL','model integration'],
 'executed':len(observed),'passed':len(observed),'static_or_pending':len(static_cases),'cases':observed})

ref=deepcopy(observed[0]['response']['snapshot']); ref.update(digest=observed[0]['snapshot_digest'])
request={'request_id':'ops03-synthetic-example-1','profile':'operations_store','question':'2026年11月支付了多少分？',
 'locale':'zh-CN','schema_revision':SCHEMA_REV,'schema_digest':schema_digest,'metric_revision':METRIC_REV,
 'schema':schema_for_model,'snapshot':ref,
 'parameters':{'start_at':START,'end_at':END,'as_of':CLOCK},'limits':limits}
query={'request_id':request['request_id'],'kind':'query','profile':'operations_store',
 'schema_revision':SCHEMA_REV,'schema_digest':schema_digest,'metric_revision':METRIC_REV,
 'sql':"SELECT COALESCE(SUM(paid_minor), 0) AS payment_minor FROM ops_order WHERE status = 'paid' AND paid_at >= :start_at AND paid_at < :end_at AND paid_at <= :as_of",
 'parameter_names':['start_at','end_at','as_of'],'clarify':None,'unsupported':None,'error':None,
 'service':{'model_identifier':'Qwen/Qwen3-8B-Base-derived','revision':'example-placeholder-not-deployed'}}
clarify=deepcopy(query);clarify.update(kind='clarify',sql=None,parameter_names=[],clarify={'slots':[{'name':'refund_rate_basis',
 'question':'按支付批次的退款订单率，还是退款金额率？','options':['cohort_refund_order_rate','cohort_refund_amount_rate']}]})
unsupported=deepcopy(query);unsupported.update(kind='unsupported',sql=None,parameter_names=[],unsupported={'reason_code':'capability_unavailable','message':'当前查询能力不可用'})
error=deepcopy(query);error.update(kind='error',sql=None,parameter_names=[],error={'code':'schema_mismatch','message':'Schema版本或摘要不匹配'})
for response in (clarify, unsupported, error):
    response.pop('sql')
save('request-response-examples.json',{**COMMON,
 'current_API':{'status':'implemented; below body verified only through isolated service method, not live HTTP',
   'request':{'method':'GET','path':'/api/ops/v1/dashboard','query':{'snapshot_id':base['snapshot_id'],'start_at':START,'end_at':END}},
   'response':{'data':observed[0]['response']},'trusted_fixture_clock':CLOCK,
   'unsupported_current_query_selectors':['as_of','revision','schema_revision','metric_revision']},
 'transition_intent':{'status':'proposed_unimplemented_SEPARATE_from_SQL_MODEL_training',
   'response':{'kind':'metric','metric_ids':['payment_minor','refund_minor'],'parameters':{'start_at':START,'end_at':END},'sql':None}},
 'SQL_MODEL_proposal':{'status':'proposed_unimplemented_NOT_SQL_validated_or_executed',
   'direction':'02=>03 request;03=>02 candidate;02 executes and returns business result',
   'request':request,'responses':{'query':query,'clarify':clarify,'unsupported':unsupported,'error':error},
   'parameter_binding':'02 resolves/pins snapshot and clamps as_of to visible before SQL binding; generated names only, no model identity/store bind',
   'forbidden_request_fields':['DSN','password','Cookie','csrf_token','subject_id','store_id','role','caller_capabilities'],
   'execution_result_proposal':{'query_ref':'generated-by02-not-model','rows':[{'payment_minor':'60000'}],'row_count':1,
     'snapshot':ref,'requested_as_of':CLOCK,'visible_at':CLOCK,'schema_revision':SCHEMA_REV,
     'schema_digest':schema_digest,'metric_revision':METRIC_REV,'truncated':False,'unknown_reasons':[]}},
 'labeling_notes':[
   'sql string is illustrative only; no02 SQL parser/executor exists; do not use as approved SQL gold',
   'service metadata and request/Schema identifiers are server-owned, not model-generated truth',
   'cost columns omitted in public model schema; backend rechecks authorization independently',
   'as_of in SQL is effective cutoff supplied by02, not a model-owned arbitrary timestamp',
   'non-query variants omit sql; nullable clarify/unsupported/error branches shown for serializer discussion',
   'missing start/end=>clarify only missing slots; invalid range=>error invalid_window; empty scalar is normal zero/null row']})

print(json.dumps({'executed':len(observed),'passed':len(observed),'static_or_pending':len(static_cases),
 'sql_validation':'NOT_RUN_UNIMPLEMENTED','business_changes':False,'output':str(OUT)},ensure_ascii=False))
