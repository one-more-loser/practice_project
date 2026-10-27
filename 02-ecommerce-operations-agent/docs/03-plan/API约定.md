# 02开发API约定

OPS-WEB-v0.1，2026-10-23，供并行实现；最终以实际OpenAPI和契约测试为准。前缀`/api/ops/v1`，响应`{data: ...}`，错误`{error:{code,message},request_id}`。HTTP 401登录无效、403无权、404不可见、409版本/状态冲突、422输入错误、503依赖未就绪。金额整数分字符串，时间ISO带时区。写请求头`X-CSRF-Token`，修改/确认额外`Idempotency-Key`。同源Cookie，客户端不传可信身份。

## 路径

- GET `/bootstrap`（公开，不创建会话）→ `{mode:'demo'|'integration',demo_available:boolean,login_url:'/login'}`，真实模式无模拟角色入口。

- GET `/context` → `{actor_ref,store_id,display_name,capabilities:string[],csrf_token,mode:'demo'|'integration',dependencies:{identity,activity,model},clock}`。
- POST `/demo/session` `{role:'owner'|'operator'|'viewer'}` → context。仅配置开启的本机演示，生产/真实接入禁用；演示身份不算01登录。
- GET `/snapshots` → `{items:Snapshot[]}`；POST `/snapshots` 接完整Snapshot，整批接收。GET `/dashboard?snapshot_id=...&start_at=...&end_at=...` → metrics及SKU/来源/时点。
- POST `/snapshots/import-csv` 接 `{manifest:{snapshot_id,revision,store_id,source_kind,as_of,currency},skus_csv,orders_csv,refunds_csv}`，三段CSV文本，具体表头见界面/csv_import.py；整批校验定位行后原子导入。
- GET `/plans` → `{items:Plan[]}`；POST `/plans` 接Draft → Plan；GET `/plans/{id}` → Plan；PUT `/plans/{id}` `{expected_version,draft:Draft}` → 新不可变版本。
- POST `/plans/{id}/evaluate` `{expected_version}` → Plan，含evaluation。
- POST `/plans/{id}/confirm` `{expected_version,reason}` → Plan；无法确认返回409及缺口，演示模式只记录sandbox_confirmed，不签真实01执行。
- POST `/plans/{id}/cancel-approval` `{expected_version,reason}`，需plan.confirm/cost.read/CSRF/幂等，未签grant本地cancelled，已签保持cancel_pending直到01屏障结果；POST `/plans/{id}/reconcile-execution` `{expected_version}`核对未决，不把unknown当成功。
- GET `/plans/{id}/export` → Markdown完整方案，重新鉴权/敏感投影。
- POST `/assistant` `{question,snapshot_id,plan_id?}` → `{answer,mode:'deterministic',evidence:[{title,value}],missing:[]}`；未获准真实模型不调用外部API。
- GET `/activities` → `{items,dependency_status}`；POST `/plans/{id}/execution-preview` → 真实01预检或503未就绪；无本地假活动。
- GET `/monitoring?plan_id=...` → `{plan,actual?,deviations:[],freshness,status}`；POST `/retrospectives` `{plan_id,summary,actions:string[]}` → 保存引用；无实际不编业绩。

## 数据形状

Snapshot：`{snapshot_id,revision,store_id,source_kind:'synthetic'|'business_import'|'derived',as_of,currency:'CNY',skus:Sku[],orders:Order[],refunds:Refund[],execution_supported_rules?:string[]}`。

Sku：`{sku_id,title,category,base_price_minor,cost_minor:null|string,variable_fee_minor:null|string,available_qty,price_version}`。
Order：`{order_id,paid_at,status:'paid'|'unpaid'|'cancelled',channel,paid_minor,items:[{sku_id,qty,paid_minor}]}`。Refund：`{refund_id,order_id,occurred_at,status:'succeeded'|'pending'|'failed',amount_minor}`。无个人信息字段。

Draft：
```json
{
  "title":"双十一现货直降方案",
  "snapshot_id":"snapshot_demo",
  "expected_snapshot_revision":1,
  "start_at":"2026-11-11T00:00:00+08:00",
  "end_at":"2026-11-12T00:00:00+08:00",
  "selected_candidate":"direct",
  "fixed_fee_minor":"10000",
  "goal":{"min_revenue_minor":"900000","min_profit_minor":"250000","budget_minor":"100000","scenario_policy":"base"},
  "lines":[{"sku_id":"sku_A","quantities":{"low":80,"base":100,"high":120},"direct_price_minor":"9200","basket_qty":1,"coupon_minor":"800","coupon_threshold_minor":"10000"}],
  "resources":[{"role":"客服","volume_per_unit":"0.3","productivity_per_hour":"20","people":1,"shift_hours":"8","attendance":"1","efficiency":"0.75","source":"模拟排班v1","confirmed":true}],
  "tasks":[{"title":"发布前价格与库存复核","owner":"店长","due_at":"2026-11-10T18:00:00+08:00","stage":"before_publish","done":true},{"title":"活动期间客服值守","owner":"客服班组","due_at":"2026-11-11T00:00:00+08:00","stage":"during","done":false}],
  "monitoring":{"interval_minutes":5,"data_source":"01.operations_snapshot","stop_conditions":["预算额度不足时停止新优惠订单"]},
  "notes":"依据与额外假设"
}
```

Plan：`{plan_id,version,store_id,status,source_kind,draft:Draft,evaluation:null|Evaluation,approval:null|object,created_at,updated_at}`。

Evaluation：`{candidates:[{id,label,scenarios:{low:Calculation,base:Calculation,high:Calculation},calculation_passed:boolean,rule_supported:boolean}],selected_candidate,feasibility:'needs_data'|'infeasible'|'checks_passed',issues:string[],resources:ResourceResult[],task_checks:Check[],lineage:object}`。
Calculation：`{revenue_minor,gross_profit_minor:null|string,contribution_profit_minor:null|string,budget_used_minor,units,checks:Check[]}`。Check：`{code,label,status:'pass'|'fail'|'unknown',reason}`。ResourceResult：`{role,required_hours,available_hours,gap_hours,additional_people,source,confirmed}`，工时十进制字符串。

注：`checks_passed`是02必需检查通过，实际01预检及真实审批门禁由后端处理；不凭snapshot的execution_supported_rules就伪称真实执行就绪。UI应显示mode/dependency/source，不自己计算金额或可行状态。

实物补充：Calculation另有各情景`resources/resource_checks`；Plan含`validity`，新修订/超24h标invalidated，已有grant维持冻结提示。桥接口见[实际契约](../04-integration/02实际执行契约.md)，真实预检status=`passed`。viewer看板/快照不含成本，方案列表只有元信息，详情/导出/监控要求cost.read。
