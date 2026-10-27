# dev D03/D04 旧结果与修补记录（2026-10-24）

保留旧证据，不覆盖 `data/sft/dev.jsonl` 与历史运行。

## D03（同一业务族）

旧 dev 标签使用四状态 paid cohort + `fulfillment_status='unshipped'`，在 fx_a
返回 **O1/O6/O7/O9**；其中 O6/O7 已全额退款，不应算“已支付未发货”。
旧 train 同类标签使用 `trade_status='paid'` + `unshipped`，返回 **O1/O9**。

已按 01/用户确认口径统一 dev D03 为：

```sql
SELECT order_id FROM cs_order_self
WHERE created_at >= :start_at AND created_at < :end_at
  AND trade_status = 'paid' AND fulfillment_status = 'unshipped'
```

新执行结果（fx_a）：**O1/O9**。旧结果 O1/O6/O7/O9 保留在此，不写成金标。

## D04（计数任务）

旧 SQL（01 接受、03 曾以 `ambiguous_grain` 拒绝）：

```sql
SELECT count(*) AS n FROM (
  SELECT DISTINCT s.order_id
  FROM cs_shipment_self s JOIN cs_order_self o ON s.order_id = o.order_id
  WHERE o.created_at >= :start_at AND o.created_at < :end_at
    AND s.carrier_signed_at >= :start_at AND s.carrier_signed_at < :end_at
) t
```

03 validator 只对“外层 COUNT、内层 SELECT DISTINCT order_id 的派生表”做局部
放行；平铺 join+SUM、死 CTE、IN 子查询等负例仍拒绝（`sandbox/compare` 26 例
回归通过）。D04 保留计数和三种问法，未改成列表。

fx_a 旧/新执行结果：**0**（11 月创建且 11 月承运商签收的订单为 0）。
