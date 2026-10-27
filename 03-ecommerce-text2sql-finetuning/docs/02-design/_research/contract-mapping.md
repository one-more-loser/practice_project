# 01／02 契约与口径映射草稿（03 Text-to-SQL 用）

版本 v0.1｜只读调研草稿｜**未经 01／02 双签，不得当作已批准契约**

路径缩写：

- `[01]` = `/mnt/d/codex_project/项目实战/01-ecommerce-customer-service-agent`
- `[02]` = `/mnt/d/codex_project/项目实战/02-ecommerce-operations-agent`
- `[03]` = `/mnt/d/codex_project/项目实战/03-ecommerce-text2sql-finetuning`

证据等级（每条事实后标注）：

| 标记 | 含义 |
| --- | --- |
| 源码 | 可直接在源码文件中复现的行；本轮逐行读过 |
| 契约 | JSON／PRD 明文，但一方或双方未签收 |
| 草案 | PRD 提案，无对应实现 |
| 推断 | 由源码结构推导（例如由 DDL 推可空性、由 validator 逻辑推某写法会被拒），**未运行验证** |

本轮未做（边界声明）：未启动 01／02 服务，未执行迁移／fixture／测试，未连接数据库，未调用模型或付费 API，未修改 01／02 任何文件。因此下文所有“会拒绝／会返回”均为源码推演，不是实测结果。

---

## 0. 结论摘要

1. **01 的 SQL 面 = 6 个逻辑视图 + 5 个固定读操作 + 4 条注册指标**（`[01]/backend/src/cs_agent/query/schema.py:7-72`、`metrics.py:5-28`、`service.py:33-43`）。视图没有 `store_id`／`owner_id`／`channel`／cost 列，租户与本人范围由 RLS 绑定，不能也无法在 SQL 里写。
2. **02 的 SQL 面 = 0**。02 只有 6 行／9 个逻辑资源的**草案**（`OPS-QUERY-v0.1`，`[02]/docs/01-requirements/PRD.md:319-332`），实际实现是内存快照上的确定性指标函数（`[02]/backend/src/ops_agent/domain.py:251-308`），没有任何 SQL 解析、validator、执行器模块。
3. **最关键口径差异**：01 先用 `created_at` 圈出“本窗创建的已支付订单”，再对这些订单求 `paid_minor`／`refunded_minor`（退款按订单累计，**与退款发生时间无关**，`[01]/.../query/service.py:205-218`）；02 按 `paid_at`（支付事件）与 `occurred_at`（退款事件）分别圈现金期，另用 `paid_at` 圈支付批次算 cohort 退款率（`[02]/.../domain.py:267-295`）。同一日期区间、同一句“本期卖了多少钱／退了多少”，两边结果必然不同，见 §5。
4. **01 无法表达“本期退款”**：退款事件时间只存在于 `refund_execution.refunded_at`（`[01]/backend/src/cs_agent/db/aftersales_tables.py:125-147`），该表既不在视图里也不在 `GRANT SELECT(col)` 清单里（`[01]/backend/migrations/versions/0011_controlled_query.py:21-30`）；`refund_execution` 不在 `SCHEMA` 字典中，任何引用都会在 `qualify` 阶段被拒（`[01]/.../query/validator.py:151-157`）。
5. **日／周分桶时区不同**：01 执行器把会话时区钉成 UTC（`[01]/.../query/executor.py:20-30`），`DATE_TRUNC('day', …)` 得到的是 UTC 日；02 的日桶显式用 `ZoneInfo("Asia/Shanghai")`（`[02]/.../service.py:203-206`）。上海零点附近 8 小时的数据会落到不同日期。
6. **01 的结果不可重放**：同一“2026-11 月”标签在 12 月重新查询会变（退款把 `refunded_minor` 与 `trade_status` 一起改，见 §1.7），查询返回的 `as_of` 只是执行时刻（`[01]/.../query/executor.py:85-92`）；02 的每次计算都钉在不可变快照 `as_of` 上（`[02]/.../domain.py:253-261`）。这对“同一问题要有稳定标签”的训练／评测是硬约束。

---

## 1. 01 受控查询面（源码事实）

### 1.1 版本、指纹与参数白名单

| 项 | 值 | 位置 |
| --- | --- | --- |
| Schema 修订 | `CS-QUERY-B2-v1` | `[01]/backend/src/cs_agent/query/schema.py:4` |
| 指标修订 | `CS-METRIC-B2-v1` | `schema.py:5` |
| Schema 指纹 | `sha256(json.dumps(SCHEMA, sort_keys=True))`，随 `validate_query` 与执行结果返回 | `schema.py:73`、`[01]/.../query/executor.py:85-92` |
| 参数白名单 | `start_at`、`end_at`、`order_id`、`sku_id`、`status` | `schema.py:74` |
| 契约文件版本 | `XCS-SQL-v0.1`，状态 `01_consumer_draft_not_jointly_approved` | `[01]/contracts/sql_generation.json:2-3`（契约） |

`schema_revision` 与 `schema_digest` 必须与常量完全相等，否则 `schema_mismatch`（`validator.py:112-114`）；03 若缓存了旧摘要，会直接失败而不是“尽力执行”。这是 03 侧做版本绑定的锚点。

### 1.2 六个逻辑视图

类型是 `schema.py` 中的逻辑字符串（`TEXT`/`TIMESTAMPTZ`/`BIGINT`/`INT`）；可空性由基础表的 `col()` 默认 `nullable=False`（`[01]/backend/src/cs_agent/db/business_tables.py:31-32`）加少数显式 `nullable=True` 推出（推断，与 0011 的 SELECT 列逐列核对）。

| 视图 | 行粒度 | 列（`NULL` 表示可空） | 允许的 join 键 | 计算列／过滤 | 不可寻址 |
| --- | --- | --- | --- | --- | --- |
| `cs_order_self` | 一行一个**本人** `customer_order` | `order_id TEXT`、`created_at TIMESTAMPTZ`、`paid_at TIMESTAMPTZ NULL`、`received_at TIMESTAMPTZ NULL`、`trade_status TEXT`、`fulfillment_status TEXT`、`goods_minor BIGINT`、`discount_minor BIGINT`、`freight_minor BIGINT`、`payable_minor BIGINT`、`paid_minor BIGINT`、`refunded_minor BIGINT`、`net_paid_minor BIGINT`、`currency TEXT` | `order_id` | `net_paid_minor = paid_minor - refunded_minor`（`0011_controlled_query.py:48`） | `display_no`、`quote_id`、`cancelled_at`、`cancellation_reason`、`policy_snapshot`、`owner_id`、`store_id`、`channel` |
| `cs_order_item_self` | 一行一个订单行 | `order_item_id TEXT`、`order_id TEXT`、`sku_id TEXT`、`product_name TEXT`、`ordered_qty INT`、`paid_goods_minor BIGINT`、`refunded_qty INT`、`refunded_goods_minor BIGINT`、`remaining_qty INT`、`remaining_goods_minor BIGINT` | `order_id`、`sku_id` | `remaining_qty = ordered_qty - refunded_qty - reserved_refund_qty`；`remaining_goods_minor = paid_goods_minor - refunded_goods_minor - reserved_refund_minor`（`0011:49`） | `goods_minor`、`discount_minor`、`allocated_minor`、`unit_price_minor`、`specifications`；**无任何时间列** |
| `cs_shipment_self` | 一行一个发货单 | `shipment_id TEXT`、`order_id TEXT`、`carrier TEXT`、`status TEXT`、`shipped_at TIMESTAMPTZ`、`carrier_signed_at TIMESTAMPTZ NULL`、`tracking_updated_at TIMESTAMPTZ` | `order_id`、`shipment_id` | 无 | `tracking_no`、`tracking_events`、`shipping_snapshot_id` |
| `cs_wallet_summary_self` | 一行一个本人钱包账户 | `account_id TEXT`、`balance_minor BIGINT`、`currency TEXT`、`status TEXT` | 无（`account_id` 不在允许 join 键内） | 无 | 无余额调整入口 |
| `cs_wallet_entry_self` | 一行一条钱包流水 | `entry_id TEXT`、`kind TEXT`、`amount_minor BIGINT`、`sequence BIGINT`、`balance_before_minor BIGINT`、`balance_after_minor BIGINT`、`occurred_at TIMESTAMPTZ`、`currency TEXT` | 无 | 无 | `account_id` 不在视图里 → **无法与 summary 关联** |
| `cs_product_public` | 一行一个 `(product, sku)`（`product ⋈ sku ⋈ inventory`） | `product_id TEXT`、`sku_id TEXT`、`name TEXT`、`category TEXT`、`price_minor BIGINT`、`currency TEXT`、`available_qty INT`、`price_version INT`、`as_of TIMESTAMPTZ` | `sku_id` | `available_qty = stock_qty - reserved_qty`；`as_of = sku.updated_at`；过滤 `product.status='active' AND sku.status='active'`（`0011:53`） | `cost_minor`（列存在但未 GRANT、未投影）、`specifications`、`status`、`stock_qty`、`reserved_qty`、`external_id` |

行粒度所需的基础表事实（源码）：

- `customer_order` 主键 `order_id`，另有 `UNIQUE(quote_id)`、`UNIQUE(display_no)`（`business_tables.py:302-345`）；每个订单最多一条支付记录（`order_payment` 有 `UNIQUE(order_id)`，`business_tables.py:418-436`）。
- `order_shipment` 有 `UNIQUE(order_id)`（`business_tables.py:489`）→ 当前模型里**一单最多一个发货单**，`cs_shipment_self` 虽然名义上是“发货单粒度”，与订单是 1:0..1。
- `inventory` 主键是 `sku_id`（`business_tables.py:215-222`）→ 无“仓”维度，`available_qty` 是单点静态值，没有快照时间列；`as_of` 借用 `sku.updated_at`，不等于库存变动时间。
- `cs_product_public` 不含 `store_id`，但 RLS 仍限定在绑定店铺与批次内（`0011:15-20`）；**没有 `owner`/`role` 过滤**，即任何被绑定成 `customer` 的会话都能看全店商品目录（`0011:34-37` 只对订单/钱包/明细/发货加了 owner 条件）。
- `cs_order_item_self` 的 `remaining_qty` 语义是“**还能退多少**”（减去了 `reserved_refund_qty`，即处理中的退款占用量），不是“未履约剩余”。（推断，源自 `0011:49` 与 `order_item` 的 CHECK：`refunded_qty + reserved_refund_qty <= ordered_qty`，`business_tables.py:373-375`。）

### 1.3 会话语义：参数 → RLS 变量

| 参数 | 绑定去向 | 位置 |
| --- | --- | --- |
| `start_at`／`end_at` | `set_config('cs.start'/'cs.end', …)`；缺省为 `[now-30d, now)`，上限 366 天，要求两端 tz-aware 且 `end > start` | `validator.py:100-109`、`executor.py:98-117` |
| 会话主体／店铺／批次 | `cs.owner`＝`subject_id`、`cs.store`、`cs.batch`、`cs.generation`、`cs.role` | `executor.py:98-117` |

`cs.start/cs.end` 在 0011 的 RLS 表达式中**未被使用**（`0011:15-20`、`35`、`37` 只用到 store/batch/generation/owner/role），即时间窗是“给 03 的候选 SQL 用”的语义参数，不是服务端的强制过滤。RLS 只保证租户、批次与本人范围，不保证时间范围——03 的模型必须自己写 `created_at` 条件（推断，逐条比对 0011 表达式与 executor 绑定）。

### 1.4 validator 允许／禁止清单

| 规则 | 具体值 | 位置 | 对 03 的后果 |
| --- | --- | --- | --- |
| SQL 字节上限 | 8192 | `validator.py:113-114` | 长 CTE／大量枚举值不可行 |
| 语句形状 | 必须是单条 `exp.Select`（`parse(read="postgres")`） | `validator.py:116-119` | 禁 `VALUES`、禁多语句、禁 `CREATE`/`COPY` |
| 规模 | 节点 ≤ 512、join ≤ 3、CTE ≤ 2、`node.depth` ≤ 18 | `validator.py:121-127` | 递归 CTE 直接拒（`:128`） |
| 节点白名单 | 见 `ALLOWED`：`select/column/identifier/table/from/join/where/and/or/not/eq/neq/gt/gte/lt/lte/in/between/is/null/boolean/literal/placeholder/order/ordered/group/having/limit/offset/alias/tablealias/with/cte/subquery/count/sum/min/max/avg/coalesce/datetrunc/timestamptrunc/paren/add/sub/mul/div/neg/distinct/star/var` | `validator.py:12-64`、`:126` | **无 `case`、无 `cast`、无 `like`、无 `anonymous`（自造函数）、无 `over`（窗口函数）**；`abs`／`round`／`nullif`／`greatest`／`filter` 等均不可用 |
| 星号 | `*` 只能在 `COUNT()` 里 | `validator.py:130-131` | `SELECT *` 一律拒 |
| `DATE_TRUNC` 单位 | 只允许 `day`、`week`、`month`（`Var` 只能出现在 trunc 的 unit 位） | `validator.py:132-136` | 不能按小时／季度分桶 |
| 表名 | 必须裸标识符，禁 db/catalog，必须 ∈ `SCHEMA` | `validator.py:137-140`、`:159-163` | `public.cs_order_self`、`pg_catalog.*` 均拒 |
| 字符串字面量 | 必须 ∈ `ENUMS` | `validator.py:65-86`、`:142-143` | 枚举值：`awaiting_payment/paid/completed/cancelled_unpaid/cancelled_refunded/closed_refunded/unshipped/in_transit/received_by_customer/carrier_signed/active/frozen/initialization/topup/payment/refund/CNY/day/week/month`。**不能按 `display_no`、`product_name`、任意 ID 文本过滤** |
| 数字字面量 | 正则 `\d+(\.\d+)?` 且 ≤ 1e9 | `validator.py:144-147` | 无科学计数法、无负数字面量（负数由 `neg` 表达） |
| 占位符 | 名字必须 ⊆ `PARAMETERS` | `validator.py:148-150` | 不能发明 `:row_limit`、`:channel`、`:store_id` |
| 列解析 | `qualify(..., schema=SCHEMA, validate_qualify_columns=True)` | `validator.py:151-157` | 未在 `SCHEMA` 中的列／表全部失败（含 `cost_minor`、`owner_id`、`store_id`） |
| 聚合粒度 | 单 scope 内 `len(sources) > 1` 且该 SELECT 直接含 `Sum`/`Avg` → `ambiguous_grain` | `validator.py:164-169` | **join 后再 `SUM` 恒被拒**；只能“先按单表聚合，再连接”或用 `IN (子查询)`（见 §1.6） |
| join 条件 | 必须 `ON <col> = <col>`，两列同名且 ∈ `{order_id, sku_id, shipment_id}` | `validator.py:170-182` | 禁 `USING`、禁笛卡尔、禁区间连接、禁 `ON a.order_id = b.shipment_id` |
| 输出列 | 非空、唯一、`^[a-z][a-z0-9_]{0,79}$`、且不含 `phone/address/street/password/secret/token/owner/staff/cost_` | `validator.py:183-193` | 聚合必须起别名；别名不能带 `cost_`；两表同名列必须改名 |
| 行数 | 缺省 `LIMIT 20`；显式字面量 < 1 拒；一律截到 ≤ 50；`OFFSET` 必须是字面量且 ≤ 10000 | `validator.py:194-207` | 超过 50 行的明细查询会被**静默截断**（不报错），03 必须把“结果可能被截断”写进评测 |
| 渲染 | `%(name)s` → `:name` | `validator.py:208-211` | 03 输出用 `:name` 形式即可 |

### 1.5 executor：身份、只读、预算、截断、错误

| 约束 | 值／行为 | 位置 |
| --- | --- | --- |
| 角色 | `ctx.role != Role.CUSTOMER` → 403 `forbidden` | `executor.py:34-35` |
| 参数集合 | 必须与 `prepared.parameters` **双向相等**（多传少传都拒）→ `invalid_parameters` | `executor.py:37-41` |
| 时间参数 | 用到 `start_at`/`end_at` 时两者必填、必须 tz-aware `datetime`，否则 `invalid_range` | `executor.py:42-50` |
| 成本预算 | `settings.query_plan_cost_limit` 为 `None` 或不在 `(0, 100000]` → 503 `query_unavailable` | `executor.py:51-53`、`[01]/backend/src/cs_agent/config.py:15-16` |
| 数据库会话 | 单连接事务；`SET LOCAL search_path=csq,pg_catalog`；`statement_timeout=2000`、`lock_timeout=500`、`default_transaction_read_only=on`、`timezone=UTC`；`connect_timeout=3`；pool 2／overflow 2 | `executor.py:20-30`、`:55-56`、`:98-117` |
| 目标校验 | `verify_target(self.engine, settings, query=True)` | `executor.py:31` |
| 计划成本 | `EXPLAIN (FORMAT JSON)` → `Plan.Total Cost` 超预算 → `query_cost_exceeded` | `executor.py:57-64` |
| 行数 | `fetchmany(51)`；> 50 → `query_rows_exceeded` | `executor.py:65-67` |
| 序列化 | 列名以 `_minor` 结尾 → `str`；`datetime` → `isoformat`；`Decimal` → `str`；行键顺序必须等于 `prepared.columns` | `executor.py:69-84` |
| 返回体 | `{rows, schema_revision, schema_digest, plan_cost, source: "controlled_postgresql", as_of}` | `executor.py:85-92` |
| 失败 | 任何异常 → 503 `query_unavailable`（“查询超时或暂不可用”） | `executor.py:95-96` |

注意：`EXPLAIN` 不带 `ANALYZE`，成本是估计值；预算 `None` 时整个 SQL 面“可用但永远 503”，是 03 联调时最常见的“看起来没接上”。

### 1.6 01 的 SQL 写法约束（03 必须内化的三条）

1. **唯一被官方测试肯定的聚合写法**是“CTE 内过滤单表 → 外层对 CTE 聚合”：
   `WITH x AS (SELECT order_id, paid_minor FROM cs_order_self WHERE created_at>=:start_at AND created_at<:end_at) SELECT count(*) AS order_count, coalesce(sum(paid_minor),0) AS paid_minor FROM x`（`[01]/backend/tests/unit/test_sql_validator.py:43-51`）。
2. **join 后求和无解**：`SELECT sum(o.paid_minor) … JOIN cs_order_item_self i ON i.order_id=o.order_id` 被显式列入“必须拒绝”的参数化用例（`test_sql_validator.py:33`），源码原因在 `validator.py:164-169`。
3. 需要“按订单时间窗过滤的 SKU 聚合”时，按源码推演只能用 `IN (子查询)` 而不是 JOIN（外层 scope 只有 1 个 source，不触发 `ambiguous_grain`）：
   ```sql
   SELECT sku_id AS sku_id, sum(paid_goods_minor) AS paid_goods_minor
   FROM cs_order_item_self
   WHERE order_id IN (
     SELECT order_id FROM cs_order_self
     WHERE created_at >= :start_at AND created_at < :end_at
       AND trade_status IN ('paid','completed','cancelled_refunded','closed_refunded')
   )
   GROUP BY sku_id
   ```
   该写法**未实测**（推断）；训练／评测前应先在 01 的 validator 单测里补一条用例确认，再进数据集。

第四个隐含坑：`sources > 1` 也包含“FROM 子查询 + 表”，所以 `SELECT sum(...) FROM cs_order_item_self i JOIN (SELECT …) o ON …` 同样被拒。

### 1.7 01 结果随时间漂移的三个具体机制

| 机制 | 机制说明 | 位置 | 后果 |
| --- | --- | --- | --- |
| 退款累加到订单 | 退款成功写 `refund_execution`（`refunded_at=now`）并 `update orders set refunded_minor=total`；全额退款把 `trade_status` 改成 `closed_refunded` | `[01]/backend/src/cs_agent/aftersales/refunds.py:112-146` | 同一历史窗口的 `refunded_minor`、`net_paid_minor`、`trade_status` 会随后续退款改变 |
| 未支付取消也写退款 | 取消已支付订单写 `refunded_at=now` 与 `refunded_minor=paid_minor` | `[01]/backend/src/cs_agent/cancellation/service.py:556-565` | 同上，且该路径不经售后域 |
| 无快照钉住 | 查询只用 RLS 的“当前 active 批次”，返回体 `as_of` 是执行时刻 | `0011:18-20`、`executor.py:85-92` | 无法“按 11 月 30 日截止”重放；训练标签会随数据演进而失效 |

综合看：01 的 `refunded_minor` 是“该订单**截至查询时刻**的累计退款”，既不是现金期退款，也不是某个 as_of 下的批次退款。

---

## 2. 01 的四个指标

### 2.1 注册表（`[01]/backend/src/cs_agent/query/metrics.py`）

| 键 | 定义原文（节选） | time_field | 金额单位 | 行号 |
| --- | --- | --- | --- | --- |
| `orders.count` | orders created in the requested half-open time window, **excluding unpaid and unpaid-cancelled orders**；grain = one row per customer_order | `created_at` | 无 | `metrics.py:5-10` |
| `orders.paid_minor` | sum of **original** `paid_minor` for paid orders created in window | `created_at` | CNY minor | `metrics.py:11-16` |
| `orders.refunded_minor` | sum of `refunded_minor` for those same paid orders, **independent of refund-entry count** | `created_at` | CNY minor | `metrics.py:17-22` |
| `orders.net_paid_minor` | sum of `paid_minor - refunded_minor` for those same paid orders | `created_at` | CNY minor | `metrics.py:23-28` |

### 2.2 唯一实现入口：`orders.summary`

`QueryService.read` 只认 5 个操作名（`orders.list`、`orders.detail`、`orders.summary`、`shipments.list`、`wallet.summary`，`service.py:33-43`），指标只在 `_summary` 里落地（`service.py:193-238`）：

| 环节 | 实现 | 位置 |
| --- | --- | --- |
| 入参 | 仅 `start_at`、`end_at` | `service.py:194` |
| 范围 | `owner + store + batch + generation` 四重等值 | `service.py:57-64` |
| “已支付”过滤 | `trade_status IN ('paid','completed','cancelled_refunded','closed_refunded') AND paid_minor > 0` | `service.py:205-210` |
| 窗口 | `created_at >= start AND created_at < end` | `service.py:218` |
| 指标 | `count()` → `order_count`；`coalesce(sum(paid_minor),0)`；`coalesce(sum(refunded_minor),0)`；`net_paid = paid - refunded` | `service.py:213-217`、`:236` |
| 响应 | `metrics_revision`、`definitions`、`window{start_at,end_at,end_inclusive:false}`、`order_count`、`paid_minor`、`refunded_minor`、`net_paid_minor`、`currency:"CNY"` | `service.py:225-238` |

### 2.3 注册表与实现的偏差（03 选标签时必须二选一）

1. **键名不一致**：注册表键是 `orders.count`，响应字段是 `order_count`（`metrics.py:5` vs `service.py:214`、`:228-231`）。03 的 Schema／字段字典要明确用哪一个，不要两头都引。
2. **“订单数”其实只是“已支付订单数”**：定义说“排除未支付与未支付取消”，实现直接限定 4 个已支付状态（`service.py:205-210`）。“本窗创建的订单总数（含未付）”在注册表里**没有指标**，只能用原始 SQL `count(*)` 另立口径。
3. **`paid_minor > 0` 是实现的附加条件**，注册表文本没有；`awaiting_payment` 的 `paid_minor = 0` 因此被双重排除。
4. **`refunded_minor` 未做 as_of 截断**，取的是查询时刻的订单累计值（见 §1.7）。
5. **单位与序列化**：SQL 侧是 `BIGINT` 分，executor 把 `_minor` 列转成字符串；`net_paid_minor` 在视图里由 SQL 算（`0011:48`），在 `_summary` 里由 Python 算（`service.py:236`），两处一致但精度路径不同（都是整数，无舍入问题）。

### 2.4 01 不提供、03 也不能在 01 侧编造的指标

| 指标 | 01 现状 | 原因 |
| --- | --- | --- |
| 本期退款额（现金期） | **未实现** | 无退款事件时间列 |
| 退款率（订单口径／金额口径） | **未实现** | 无 `case`、无 `nullif`、没有 cohort 定义，且 `ambiguous_grain` 挡住 join 后聚合 |
| 支付客单价 | **未实现** | 需除零保护，SQL 面无 `nullif` |
| 商品支付销量（总件数） | 需自写：`sum(ordered_qty)` | 注册表无此指标；`cs_order_item_self` 无 `paid_qty`，`ordered_qty` 是下单件数，**不是**支付件数 |
| SKU 维度金额 | 需自写：`sum(paid_goods_minor)` | 见 §2.5 的粒度不齐 |
| 渠道／店铺维度 | **不可表达** | 两列都不在视图里 |
| 成本／毛利／贡献利润 | **不可表达** | `cost_minor` 未投影，输出列还禁 `cost_` 前缀 |

### 2.5 01 内部的粒度不齐（最容易做错的一处）

- 订单级：`payable_minor = goods_minor - discount_minor + freight_minor`，支付时 `paid_minor = payable_minor`（`business_tables.py:333-335`、`[01]/backend/src/cs_agent/commerce/checkout.py:735`）。
- 行级：`allocated_minor = goods_minor - discount_minor`（`business_tables.py:370-372`），支付时 `paid_goods_minor = allocated_minor`（`checkout.py:712-716`）。
- 结论：`Σ paid_goods_minor = paid_minor - freight_minor`（推断，两条 CHECK 直接推出）。因此“订单实付”与“行实付合计”**天然差一个运费**。03 若把 `cs_order_item_self.paid_goods_minor` 求和后拿去校验 `cs_order_self.paid_minor`，会稳定差运费。
- 同理 `net_paid_minor`（订单级、含运费）与 `remaining_goods_minor`（行级、不含运费）不可相加。

---

## 3. 02 的资源与指标

### 3.1 现状：没有 SQL 面

- `[02]/backend/src/ops_agent/` 只有 `app.py`、`auth.py`、`bridge.py`、`csv_import.py`、`db.py`、`domain.py`、`errors.py`、`seed.py`、`service.py`，**没有 query／sql／validator／executor 模块**（目录列出即事实）。
- 02 的“查询”是 `GET {PREFIX}/dashboard`（`[02]/backend/src/ops_agent/app.py:167-171`），参数 `snapshot_id`、`start_at`、`end_at`、`sku_id`、`channel`，实现在 `service.py:145-202`，指标在内存里算（`domain.py:251-308`）。
- 唯一的“版本常量”出现在 `domain.py:590`：`metric_revision: OPS-METRIC-v0.1`、`calculation_revision: OPS-CALC-v0.1`（用于方案计算），**dashboard 响应里没有 `metric_revision` 字段**（`service.py:194-202` 的 key 逐项核对）。03 若需要 02 侧口径版本，现在拿不到。
- 快照 Schema 文件 `[02]/contracts/snapshot-v0.2.json` 对 `skus`/`orders`/`refunds` 只声明 `"type": "array"`（无 item 结构），真正的字段校验在 `domain.validate_snapshot`（`domain.py:136-248`）。**02 的“schema”是代码，不是 JSON Schema。**

### 3.2 逻辑资源草案（`OPS-QUERY-v0.1`，状态：草案）

出处：`[02]/docs/01-requirements/PRD.md:319-332`。原文明确“拟版本”“不是物理表设计”“均待设计及共同契约评审”。

| 逻辑资源 | 业务粒度 | 允许字段／关联要求（原文） | 不暴露 | 03 可用性 |
| --- | --- | --- | --- | --- |
| `ops_order` | 店铺、订单 | 不含身份的订单引用、`paid_at`、主渠道、支付/退款状态；1:N `order_item` | 姓名、电话、地址、顾客标识 | 草案（无实现） |
| `ops_order_item` | 店铺、订单行 | 行引用、订单引用、SKU、数量、商品金额及原生分摊标记；N:1 `sku` | 收货/支付账户明细 | 草案 |
| `ops_payment`／`ops_refund` | 店铺、事件 | 事件引用、订单/可用行引用、时点、成功状态、商品/运费金额、`currency` | 支付凭据、退款审批私文 | 草案（02 现有实现无独立事件表，只有快照里的 `refunds[]`） |
| `ops_sku`／`ops_cost` | SKU 及价格/成本**有效段** | 规格、单位、有效价、授权成本与完整度；**按事件时点匹配唯一有效段** | 供应商隐私、采购凭据 | 草案（02 现实现是静态 `skus[]`，无有效期段） |
| `ops_inventory` | SKU/仓/快照时点 | 可用量组件、完整度、时间；禁止无时点关联历史销量 | 顾客预留明细 | 草案（现有 `available_qty` 是整数单点值，无时点序列） |
| `ops_sales_period`／`ops_traffic_period` | 店铺/SKU/主渠道/声明时间桶 | 可加总支付/退款；去重订单/访客需声明不可跨维度求和 | 用户行为明细 | 草案（访问/访客数据根本没有来源） |

同一节还要求共同包必须带“字段类型/可空性/枚举、关系基数、金额单位、分摊方式、**时区**、去重和未知值、`cost.read` 投影、权限执行责任、版本兼容规则、正常/空/缺失/迟到/重复/越权样本”，并写明“未完成前不得生成针对全物理库的 SQL”（`PRD.md:331`）。**这条对 03 同样生效**：不能把 `ops_*` 当成已存在的表去训练。

### 3.3 02 实际输出的指标（源码事实，非草案）

`calculate_metrics` 返回值（`domain.py:298-308`）：

| 字段 | 定义（源码行为） | 时间基准 | 空值／未知 | 行号 |
| --- | --- | --- | --- | --- |
| `payment_minor` | Σ `order.paid_minor`，仅 `status=='paid'` 且 `paid_at ∈ [start,end)` 且 `paid_at <= visible` | 支付事件 `paid_at` | 无单 → `"0"`（`_amount(0)`） | `domain.py:267-276`、`:299` |
| `paid_order_count` | 上述 cohort 的订单数 | 同上 | 0 | `domain.py:300` |
| `paid_units` | cohort 订单的 `Σ item.qty` | 同上 | 0 | `domain.py:274-276`、`:301` |
| `average_order_minor` | `payment / count`，6 位小数后转分 | 同上 | 分母 0 → `None` | `domain.py:301` |
| `refund_minor` | Σ `refund.amount_minor`，`status=='succeeded'` 且 `occurred_at ∈ [start,end)` 且 `occurred_at <= visible` | 退款事件 `occurred_at` | 指标级可置 `None`（见 §3.4） | `domain.py:285-292`、`:299` |
| `net_paid_minor` | `payment_minor - refund_minor`，**可为负** | 混合：支付事件窗 − 退款事件窗 | 同上 | `domain.py:300`；规则见 `PRD.md:220` |
| `cohort_refund_order_rate` | `distinct(有任意成功退款的 cohort 订单) / count`，6 位小数、`ROUND_HALF_UP` | cohort = `paid_at` 在窗内的订单，**退款不限时点**（只受 `visible` 截断） | 分母 0 → `None` | `domain.py:293-295`、`:302-303` |
| `cohort_refund_amount_rate` | `cohort_refund / payment`，6 位小数 | 同上 | `payment <= 0` → `None` | `domain.py:304-305` |
| `channels[]` | 按 `order.channel` 分组的 `payment_minor` + `paid_order_count`；空白→`"unknown"` | 支付事件窗 | 缺失渠道归 `unknown` | `domain.py:276-279`、`:306-307` |

精度常量：`CENT = Decimal(1)`、`RATIO = Decimal("0.000001")`（`domain.py:20-21`），金额 `_amount()` 用 `ROUND_HALF_UP` 定点到分（`domain.py:118-121`）。

### 3.4 02 的时间／未知值／权限口径

| 维度 | 02 的做法 | 位置 |
| --- | --- | --- |
| 时区 | 业务时区 Asia／Shanghai；窗口是**瞬时**比较（tz-aware 两端），日桶显式转上海本地日 | `PRD.md:218`、`service.py:203-206` |
| 窗口 | `[start,end)` 半开；`start >= end` → `invalid_window`；缺省 `end=now`、`start=end-30d` | `domain.py:259-260`、`service.py:173-174` |
| as_of | 快照 `as_of` 为 watermark；`visible = min(查询 as_of, watermark)`，支付与退款都必须 `<= visible` | `domain.py:253-261`；`service.py:175`、`:187` |
| 迟到／修订 | 每次导入是新的不可变 `(store_id, snapshot_id, revision)`；同修订内容不同 → `revision_conflict` | `service.py:99-106`；`[02]/backend/src/ops_agent/db.py:22-27` |
| 未知值 | 多 SKU 订单退款缺原生行分摊时：现金期未知则 `refund_minor`、`net_paid_minor` 置 `None`；cohort 未知则两个 rate 置 `None`；并给 `filter_missing` 说明 | `service.py:177-193`、`:202` |
| 无数据 vs 真 0 | 指标字段本身不区分“无事件”与“0”（`_amount(0)` = `"0"`），只有比率用 `None` 表示分母为 0 | `PRD.md:347`；`domain.py:299-305` |
| 成本权限 | 能力名 `cost.read`；`owner`/`operator` 有、`viewer` 只有 `metrics.read`；无能力时快照剔除 `cost_minor`/`variable_fee_minor` | `[02]/backend/src/ops_agent/auth.py:13-18`、`service.py:131-133`、`:135-143`、`:228-234`、`:296-297` |
| 利润 | 即使有 `cost.read` 也只给 `contribution_profit_minor: null` + `profit_missing_reason` | `service.py:228-232` |
| 订单状态 | `paid`／`unpaid`／`cancelled` 三值；退款 `succeeded`／`pending`／`failed` | `domain.py:206`、`:234` |
| 快照结构约束 | 订单必填 `{order_id, paid_at, status, channel, paid_minor, items}`，`Σ item.paid_minor == order.paid_minor`（含运费）；行的 SKU 必须存在；`succeeded` 退款要求原单 `paid` 且 `occurred_at >= paid_at`；成功退款累计 ≤ `paid_minor`；币种必须 CNY | `domain.py:204-225`、`:228-246`、`:175` |
| 没有的字段 | 快照**没有订单创建时间**，也没有 `order_item_id`、商品原价/折扣、运费拆分、退货数量 | `domain.py:204`、`:218-222` |

### 3.5 02 草案与实现的落差（03 不能引用为已实现）

| PRD 主张 | 实现现状 |
| --- | --- |
| 价格/成本“有效段”，按事件时点匹配 | 只到 `price_version` 整数，无 `valid_from/valid_to`（`domain.py:188-199`） |
| `ops_payment`／`ops_refund` 独立事件资源 | 只有快照 JSON 里的数组，无表、无 SQL |
| 库存按快照时点、可关联历史 | 单点 `available_qty`，无 `snapshot_at`（`domain.py:197-198`） |
| 转化率、渠道去重访客 | 无访问数据，未实现（`PRD.md:234`） |
| 商品毛利率 | 未实现；利润恒 `null`（`service.py:231-232`） |
| `OPS-METRIC-v0.1` 作为响应字段 | dashboard 响应无 metric revision（`service.py:194-202`） |

---

## 4. 01 与 02 的时间口径对照（核心）

### 4.1 一句话

**01 是“按建单时间圈订单，再看这批订单现在的累计支付／累计退款”；02 是“按支付事件圈支付、按退款事件圈退款，两者各用各的时间列”。** 一个以订单为锚，一个以事件为锚。

### 4.2 逐维度对照

| 维度 | 01（`cs_order_self` 口径） | 02（`calculate_metrics` 口径） | 后果 |
| --- | --- | --- | --- |
| 圈选锚点 | `created_at`（建单时刻，`checkout.py:533-555` 用 `provenance(ctx, now)` 写入，`[01]/backend/src/cs_agent/actions/repository.py:17-26`） | 支付 `paid_at`、退款 `occurred_at`（`domain.py:270`、`:291`） | 同一“11 月”，01 数的是 11 月**下单**的订单，02 数的是 11 月**收款**的订单 |
| 跨期订单 | 10 月下单、11 月支付 → 计入 10 月窗口 | 同单 → 计入 11 月窗口 | 月末／大促前后差异最大 |
| 退款时点 | **不使用**；退款按订单累计，全部归属建单窗口 | 使用 `occurred_at`：现金期退款只看本期退款事件，可来自任意历史订单（`PRD.md:230`） | 01 的 `refunded_minor` 与 02 的 `refund_minor` **不是同一个东西** |
| 批次退款率 | 无定义、无实现 | `paid_at` 圈 cohort，退款截至 `visible`，不受退款时点限制（`domain.py:293-295`） | 只有 02 能表达“支付批次退款率”，但 02 无 SQL 面 |
| 分母／去重 | `count(*)` over 已支付订单 | `len(cohort)` = distinct `order_id` 且 `status=='paid'` | 01 的 4 状态集合 ⊃ 02 的单状态集合（01 计入 `cancelled_refunded`/`closed_refunded`） |
| as_of | 无；响应的 `as_of` 是执行时刻（`executor.py:85-92`） | 快照 watermark + 查询 `as_of` 取 `min`（`domain.py:253-261`） | 01 不可重放，02 可重放 |
| 时区／日桶 | 会话 `timezone=UTC`（`executor.py:20-30`），`DATE_TRUNC('day')` 是 UTC 日 | 日桶 `ZoneInfo("Asia/Shanghai")`（`service.py:203-206`） | 上海 00:00–08:00 的事件在 01 落到前一天 |
| 单位 | CNY 分（SQL `BIGINT`，输出字符串） | CNY 分（整数字符串） | 一致，可直接对齐 |
| 状态枚举 | 6 值：`awaiting_payment/paid/completed/cancelled_unpaid/cancelled_refunded/closed_refunded`（`business_tables.py:340-342`） | 3 值：`paid/unpaid/cancelled`（`domain.py:206`） | 需要显式映射表，不能同名直连 |
| 退款状态 | `refund_execution` 无状态列（写成功才落库，`refunds.py:112-126`）；`refund_claim` 有 8 个流程状态（`aftersales_tables.py:53-55`） | `succeeded/pending/failed`（`domain.py:234`） | 01 的“成功退款”= 存在 `refund_execution` 行；02 还要看 `status` 字段 |
| 粒度 | 订单级含运费；行级不含运费（§2.5） | 订单级 `paid_minor` **必须等于**行合计（`domain.py:223`） | 01 的行金额与订单金额天然不等，02 天然相等 |

### 4.3 同一句问题、同一窗口的两个答案（按源码语义推演，未运行）

窗口 `W = [2026-11-01T00:00+08:00, 2026-12-01T00:00+08:00)`；数据：

| 订单 | 建单 | 支付 | 支付额（分） | 退款 |
| --- | --- | --- | --- | --- |
| A | 10-31 23:50 | 11-01 00:05 | 10000 | 无 |
| B | 11-15 10:00 | 11-15 10:01 | 30000 | 11-20 成功退 10000 |
| C | 11-20 10:00 | 未支付，超时 `cancelled_unpaid` | 0 | 无 |
| D | 11-25 09:00 | 11-25 09:01 | 20000 | 12-05 成功退 20000（全额，转 `closed_refunded`） |

**01 在 12-10 查询 W**（`created_at` 圈单 + 4 状态 + `paid_minor>0`）：

- cohort = {B, D}（A 不在窗口；C 是 `cancelled_unpaid`）；`order_count = 2`
- `paid_minor = 30000 + 20000 = 50000`
- `refunded_minor = 10000 + 20000 = 30000`（D 的退款发生在窗口外，仍然算进 11 月的 `refunded_minor`）
- `net_paid_minor = 20000`
- 若改在 11-30 查询：D 还没退款，`refunded_minor = 10000`、`net_paid_minor = 40000`。**同一标签，两个值。**

**02 查询 W，`as_of = 2026-12-10`**：

- 支付：A（11-01 00:05） + B + D = 60000；`paid_order_count = 3`（A 是 10 月建单）
- 现金期退款：只有 B 的 10000（D 的退款 `occurred_at` 在 12-05，不在 W 内，即使可见也不计）→ `refund_minor = 10000`
- `net_paid_minor = 50000`
- cohort（`paid_at ∈ W` = {A,B,D}）截至 12-10 的退款 = 10000 + 20000 = 30000 →
  `cohort_refund_order_rate = 2/3 = 0.666667`；`cohort_refund_amount_rate = 30000/60000 = 0.500000`

| 同一问题的答案 | 01 | 02 |
| --- | --- | --- |
| 支付额 | 50000 | 60000 |
| 退款额 | 30000 | 10000 |
| 净支付 | 20000 | 50000 |
| 已支付订单数 | 2 | 3 |
| 退款率 | 无定义 | 0.666667（订单）／0.500000（金额） |

结论：**“本期销售额”“本期退款”“退款率”这三个词在 01 与 02 之间没有共享定义**。03 若同一份训练／评测标签混用，模型会学到互相矛盾的映射，且离线分数无法解释。

### 4.4 03 必须做的取舍（建议，不是既定决定）

1. **标签只能锚定一个来源**：要么“01 订单口径（建单窗 + 累计退款）”，要么“02 事件口径（支付窗 + 退款窗）”。不要在同一指标族里混。
2. 若要 02 口径，必须固化 `as_of`，并且数据来自快照而非 01 实时库；要注明“01 无法提供该口径”。
3. 若要 01 口径，必须在标签里写明“`refunded_minor` 截至执行时刻，不可重放”，并在评测里固定数据版本（`schema_digest` 不够，还需要库快照）。
4. 凡涉及“本期退款／退款率／渠道／成本”的问题族，01 应回答 `unsupported`（对应 01 契约里的 `kind=unsupported`，`[01]/docs/02-design/跨项目契约草案.md:116`），而不是硬造 SQL。

---

## 5. 字段与实体映射（01 ↔ 02）

| 语义 | 01 | 02 | 是否同义 | 差异 |
| --- | --- | --- | --- | --- |
| 订单标识 | `cs_order_self.order_id` | `orders[].order_id` | 名义同义，命名空间未对齐 | 02 明示“P0 合成映射；不自称与 01 相同 ID 已对齐”（`PRD.md:308`） |
| 建单时间 | `created_at` | **无** | — | 02 快照没有该字段 |
| 支付时间 | `paid_at`（有列，但指标不用） | `orders[].paid_at` | 同义（都是支付成功瞬时） | 01 只用于展示/过滤；02 用于圈 cohort |
| 支付额 | `paid_minor`（= `payable_minor`，含运费） | `orders[].paid_minor`（= Σ 行，含运费，`domain.py:223`） | 同义 | 都含运费 |
| 行支付额 | `paid_goods_minor`（= 商品-折扣，**不含运费**） | `items[].paid_minor`（合计=订单额，**含运费分摊**） | **不同义** | 01 行额 ≠ 订单额；02 行额 = 订单额 |
| 退款额 | `refunded_minor`（订单级累计） | `refunds[].amount_minor`（事件级）+ 现金期/cohort 两个聚合 | **不同义** | 粒度、时间语义都不同 |
| 退款时间 | 无（`refund_execution.refunded_at` 不可见） | `refunds[].occurred_at` | — | 01 缺字段 |
| 订单状态 | 6 值 `trade_status` | 3 值 `status` | 需映射 | `paid`→`paid`；`awaiting_payment`/`cancelled_unpaid`→非 paid；`completed`/`cancelled_refunded`/`closed_refunded`→`paid`（推断，02 无对应状态） |
| 退款状态 | 无列（成功才落 `refund_execution`） | `refunds[].status` 3 值 | 需映射 | 02 的 `pending`/`failed` 在 01 的 SQL 面不可见 |
| 商品价格 | `cs_product_public.price_minor`（当前常规价） | `skus[].base_price_minor` | 近似同义 | 01 无有效期、无活动价；02 有 `price_version` 但无有效段 |
| 库存 | `available_qty = stock - reserved`，`as_of = sku.updated_at` | `skus[].available_qty`（整数、可空） | 近似同义 | 01 无仓、无快照时点；02 可空表示未知 |
| 成本 | **不可见**（列存在、未投影、别名禁用 `cost_`） | `cost_minor` + `variable_fee_minor`，`cost.read` 保护 | 01 缺字段 | 01 增接口也不会产生可靠成本（`PRD.md:302`） |
| 渠道 | **无列** | `orders[].channel`（空→`unknown`） | 01 缺字段 | 渠道维度只有 02 有 |
| 店铺 | 隐式（RLS `cs.store`） | `snapshots.store_id` | 同义但不可见 | 两边都不可在结果里跨店聚合 |
| 币种 | `currency`（CHECK 固定 `CNY`） | `currency`（必须 `CNY`，`domain.py:175`） | 同义 | 均为 CNY |
| 批次／版本 | `data_batch_id` + `batch_generation`（RLS） | `snapshot_id` + `revision` | 概念相近、编号体系不同 | 没有共享的批次标识，不能互相校验 |

---

## 6. 问题族 → 资源／字段／指标／参数／禁用项

状态列：**已实现**＝01/02 现有代码可直接产出；**草案**＝有 PRD 定义无实现；**未实现**＝两边都没有。

| # | 问题族（示例问法） | 01 资源／字段／表达式 | 02 资源／指标 | 参数 | 禁用项与风险 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| F1 | 这段时间我下了多少单 | `cs_order_self`；`count(*) AS order_count`；`created_at` 半开窗 | `ops_order`；无对应指标（02 无建单时间） | `start_at`、`end_at` | 不可用 `orders.count`（那是“已支付订单数”）；`count(*)` 必须别名 | 01 已实现（自写 SQL）；02 不可答 |
| F2 | 这段时间卖了多少（支付额） | `coalesce(sum(paid_minor),0)` + 4 状态 + `paid_minor>0` | `payment_minor`（`paid_at`） | `start_at`、`end_at` | 01 口径 = 建单窗；02 口径 = 支付窗；标签必须二选一 | 01 已实现；02 已实现（无 SQL） |
| F3 | 这段时间退了多少 | **无法精确表达**（只有订单累计 `refunded_minor`） | `refund_minor`（`occurred_at`） | `start_at`、`end_at` | 01 没有任何退款时间列；写 `sum(refunded_minor)` 得到的是“本窗建单订单的累计退款”，不是本期退款 | 01 未实现；02 已实现（无 SQL） |
| F4 | 净支付／净实付 | `net_paid_minor` 列或 `sum(paid_minor-refunded_minor)` | `net_paid_minor`（可为负） | `start_at`、`end_at` | 01 净额恒 ≥ 0（CHECK `refunded_minor<=paid_minor`，`business_tables.py:336`）；02 可为负 | 01 已实现；02 已实现 |
| F5 | 退款率 | **不可表达**（无 `case`、无 `nullif`、join 后聚合被拒） | `cohort_refund_order_rate`／`cohort_refund_amount_rate` | `start_at`、`end_at`（+`as_of`） | 01 侧若用 Python 拼 CASE 会直接被 validator 拒；分母 0 必须返回未知 | 01 未实现；02 已实现（无 SQL）；03 未实现 |
| F6 | 各 SKU 销售额／销量 | `cs_order_item_self.paid_goods_minor`／`ordered_qty`，`GROUP BY sku_id` | `ops_order_item`；`paid_units` 只有总数 | `start_at`、`end_at` | **join 订单 + `SUM` 必被拒**；须用 `IN (子查询)`（§1.6，推断）或先聚合后连接；行额不含运费；`ordered_qty` 不是支付件数 | 01 部分可用（受 join 限制）；02 草案 |
| F7 | 商品价格／库存 | `cs_product_public`（`price_minor`、`available_qty`、`as_of`） | `ops_sku`／`ops_inventory` | 无时间参数（`as_of` 只读） | 价格是常规价、非活动价；库存无仓、无时点序列；`cost_minor` 引用即失败 | 01 已实现；02 草案 |
| F8 | 某一单的明细 | `order_id` 参数 + `cs_order_self` + `cs_order_item_self` | `ops_order` + items | `order_id` | `order_id` 只能作占位符，**不能写字面量**（字符串白名单不含 ID）；两表 join 只能 `ON order_id`；输出列需改名避免重名 | 01 已实现 |
| F9 | 发货／物流到哪了 | `cs_shipment_self`（`carrier`、`status`、`shipped_at`、`carrier_signed_at`） | 无对应资源 | `order_id`、`status` | 一单最多一个发货单；`tracking_no` 不暴露；`status` 只有 `in_transit`/`carrier_signed` | 01 已实现；02 未实现 |
| F10 | 钱包余额／流水 | `cs_wallet_summary_self` + `cs_wallet_entry_self` | 无 | `start_at`、`end_at`（流水用 `occurred_at`） | 两视图**无法互连**（`account_id` 不在 join 白名单，且 entry 视图无 `account_id`）；只能各查一次 | 01 已实现；02 未实现 |
| F11 | 成本／毛利／利润 | **不可寻址** | `ops_cost` + `cost.read`；`contribution_profit_minor` 恒 `null` | 无 | 01 引用 `cost_minor` 在 `qualify` 阶段失败；输出别名不得含 `cost_` | 01 未实现；02 草案 |
| F12 | 各渠道表现 | **不可表达**（无 `channel` 列） | `channels[]`（`payment_minor` + 订单数） | `channel` 过滤（02） | 01 只能答 `unsupported`；02 的 `channel` 是订单主渠道，空→`unknown` | 01 未实现；02 已实现（无 SQL） |
| F13 | 按天／按周趋势 | `DATE_TRUNC(unit, created_at)`，`unit` 只能是 `day`/`week`/`month`；UTC 日 | `history[]`（上海日） | `start_at`、`end_at` | 时区不同（§4.2）；`DATE_TRUNC` 单位只能是 `day/week/month`；不能 `GROUP BY` 表达式别名以外的形式（需实测） | 01 已实现（UTC 桶）；02 已实现（上海桶） |
| F14 | 客服／会话／工单 | 不在 SQL 面（`support` 域） | FR-OPS-015 消费方 | — | 周汇总契约 `XCS-SUPPORT-v0.1` 是“已结束周、周一 00:00 上海”，**与 SQL 窗口无关** | 两边均未实现 |
| F15 | 活动／促销效果 | `activities` 域不在 SQL 面 | 02 的 V1 执行／监控 | — | 方案测算与经营指标分属两个视图（`service.py:294-298`），且 `PRD.md:342` 要求结果标注方案/口径/计算版本 | 均未实现 |

### 6.1 跨族禁用清单（03 应写进 system prompt 与评测反例）

1. 禁写：任何 `INSERT/UPDATE/DELETE/CREATE/COPY/CALL`、多语句、`SELECT *`、`pg_catalog`／`public.` 前缀。
2. 禁函数：`CASE`、`CAST`、`LIKE`、窗口函数（`OVER`）、`NULLIF`、`ROUND`、`ABS`、`GREATEST`、`FILTER`、任何自造/未列名函数；只能用 `count/sum/min/max/avg/coalesce/date_trunc/timestamp_trunc` 与算术。
3. 禁字段：`cost_minor`、`owner_id`、`store_id`、`channel`、`display_no`、`phone`、`address`；以及一切别名含 `phone/address/street/password/secret/token/owner/staff/cost_` 的输出。
4. 禁字面量：任意 ID／商品名／日期的字符串字面量（会被 `ENUMS` 拒），只允许 16 个枚举值 + `CNY` + `day/week/month`。
5. 禁形状：join 后聚合、`JOIN … ON` 非同名键或非 `order_id/sku_id/shipment_id`、`USING`、三表以上、深于 18 层、`LIMIT > 50`、`OFFSET > 10000`。
6. 禁跨源：不得把 01 的列名与 02 的字段名混在一条 SQL 里（02 无 SQL 面）；不得声称 `ops_*` 表存在。

### 6.2 01／02 都不提供、必须由 03 自己定义的口径

| 待定义项 | 为什么必须由 03 定义 |
| --- | --- |
| 任意比率的分子／分母与精度 | 01 无比率指标且不能安全表达；02 用 6 位小数 `ROUND_HALF_UP`（`domain.py:20-21`、`:302-305`），若 03 要一致必须显式抄这个精度 |
| “本期”的时区与边界 | 01 是 UTC 日；02 是上海日；两边都不是“03 的口径” |
| 运费的归属 | 01 订单含运费、行不含；02 订单=行（含运费分摊） |
| 多 SKU 订单的分摊 | 01 有原生分摊（`paid_goods_minor`），02 只接受“行合计=订单额”，无“缺分摊”表达（02 在 dashboard 层才用 `ambiguous` 处理，`service.py:158-172`） |
| 未知值的表示 | 01 只能返回 NULL 或不出现在结果里；02 用 `null` + `filter_missing`；03 需要统一 |
| 空结果 vs 真 0 | 01 `coalesce(sum,0)` 会把“没有订单”与“金额为 0”都变成 `0`；02 同样不区分（`PRD.md:347` 要求区分） |

---

## 7. 版本、漂移与待确认

### 7.1 版本常量一览

| 侧 | 标识 | 值 | 位置 | 状态 |
| --- | --- | --- | --- | --- |
| 01 | Schema 修订 | `CS-QUERY-B2-v1` | `[01]/.../query/schema.py:4` | 源码生效 |
| 01 | 指标修订 | `CS-METRIC-B2-v1` | `schema.py:5` | 源码生效 |
| 01 | 对外契约 | `XCS-SQL-v0.1` | `[01]/contracts/sql_generation.json:2` | 契约，`01_consumer_draft_not_jointly_approved` |
| 01 | 客服周汇总 | `XCS-SUPPORT-v0.1`（`timezone: Asia/Shanghai`，周一口径） | `[01]/contracts/support_summary.json` | 契约，与 SQL 面无关 |
| 02 | 快照 Schema | `OPS-SNAPSHOT-v0.1`／`-v0.2` | `[02]/.../domain.py:152-156` | 源码生效 |
| 02 | 逻辑查询面 | `OPS-QUERY-v0.1` | `[02]/docs/01-requirements/PRD.md:319-321` | 草案 |
| 02 | 指标口径 | `OPS-METRIC-v0.1` | `PRD.md:210`；`domain.py:590`（仅方案计算） | 草案／无对外字段 |
| 02 | 计算修订 | `OPS-CALC-v0.1` | `domain.py:590` | 源码常量，仅方案计算返回 |
| 03 | — | 尚无 | — | 待 03 PRD 定义 |

### 7.2 已发现的漂移（文档与源码不一致）

| 漂移 | 文档说法 | 源码实际 | 影响 |
| --- | --- | --- | --- |
| 样例列名 | 契约草案样例用 `original_paid_minor`（`[01]/docs/02-design/跨项目契约草案.md:110`） | 视图列是 `paid_minor`（`schema.py:18`） | 03 若照文档生成，`qualify` 阶段失败 |
| 样例参数 | 样例含 `row_limit`、`LIMIT :row_limit`（同上 `:94`、`:110-111`） | `PARAMETERS` 无 `row_limit`（`schema.py:74`），行数由 validator 常量 50 截断（`validator.py:194-207`） | 03 不能输出 `:row_limit` |
| 版本号 | 样例 `schema_revision: CS-QUERY-v0.1`、`metric_revision: CS-METRIC-v0.1`（同上 `:89-90`） | 实际 `CS-QUERY-B2-v1`／`CS-METRIC-B2-v1` | 版本校验必然失败 |
| 02 快照 Schema | JSON 只声明数组（`[02]/contracts/snapshot-v0.2.json` 的 `skus/orders/refunds: {"type":"array"}`） | 真正的字段约束在 `domain.validate_snapshot` | 只读 JSON 会以为“无字段约束” |
| 02 指标修订 | PRD 提 `OPS-METRIC-v0.1` | dashboard 响应不带该字段 | 03 拿不到 02 的口径版本 |
| 01 已消费 02 数据 | — | `[01]/docs/06-acceptance/XOPS-20261023-01/repair-R1/http-followup-20261023T185225971515Z.json` 内含 `paid_order_count`、`order_count_semantics` 等 02 dashboard 字段 | 说明 01 的验收流程已经消费过 02 形状的合成 payload，但**不是**签收过的契约；03 不可据此认为接口已联通 |

### 7.3 需要 01／02 签收的问题（建议编号进 03 的 PRD）

| 编号 | 问题 | 涉及方 |
| --- | --- | --- |
| Q-01 | 01 是否补退款事件视图（`refund_execution.refunded_at` + 状态）？不补则 03 永远无法在 01 口径回答“本期退款／退款率” | 01 |
| Q-02 | 01 的 `orders.count` 是否改为“本窗创建订单数（含未付）”，与实现一致？还是把实现改成返回两个字段？ | 01 |
| Q-03 | 01 是否放开 join 后聚合（或提供 `orders.summary_by_sku` 固定操作）？当前规则使“按 SKU 的时间窗聚合”只能靠子查询绕行 | 01 |
| Q-04 | 01 的会话时区能否按请求设成 `Asia/Shanghai`，或提供 `date_trunc` 的时区参数？否则日／周桶与 02、与业务预期都不一致 | 01 |
| Q-05 | 01 能否提供“按 as_of 重放”（批次快照或只读副本）？否则 03 的历史标签不可复现 | 01 |
| Q-06 | 02 是否把 dashboard 的 `metric_revision`／`snapshot_id`／`revision`／`as_of` 作为必备响应字段？现在只有 `snapshot` 子对象含部分 | 02 |
| Q-07 | 02 的 `OPS-QUERY-v0.1` 是否只作为“内部 SQL 面”而非跨项目契约？03 是否应改为“只对 01 出 SQL，对 02 只做指标问答（无 SQL）” | 02／03 |
| Q-08 | 跨项目的 `order_id`／`sku_id` 命名空间是否对齐？02 明示未对齐（`PRD.md:308`） | 01／02 |
| Q-09 | 运费、优惠分摊、多 SKU 订单的“唯一真值”归属哪一侧？01 与 02 的行金额定义不同（§5） | 01／02 |

---

## 8. 对 03 的落地建议（不含决策）

1. **先决定“单一真值源”**：训练／评测标签若来自 01，就在 Schema 里写死 01 的 6 个视图与 4 条指标；不要同时声称支持 02 的指标名。反之亦然。
2. **把 §6.1 的禁用清单变成评测反例集**：每条都能在 01 的 validator 源码里找到拒绝理由（§1.4 已给行号），可直接写 unit test。
3. **比率与时间桶不要放进 SQL**：01 无 `nullif`／`round`，`DATE_TRUNC` 只有三档且是 UTC；比率与时区转换应在 03 侧后处理，SQL 只负责可加总的金额与计数。
4. **标签必须带 `schema_digest`＋数据版本**：01 的 `as_of` 只是执行时刻（§1.7），仅靠它无法复现；至少记录 `data_batch_id`／`batch_generation`（可由 01 的 RLS 变量推断，但当前不在响应体里）。
5. **对“退款／退款率／渠道／成本”四类问题训练 `unsupported` 或 `clarify`**，并在 Schema 里写明理由（对应 01 契约的 `kind` 字段，`[01]/docs/02-design/跨项目契约草案.md:116`）。

---

## 9. 证据索引（按文件）

| 文件 | 关键行 |
| --- | --- |
| `[01]/backend/src/cs_agent/query/schema.py` | `4-5` 版本；`7-72` 六视图列；`73` 指纹；`74` 参数白名单 |
| `[01]/backend/src/cs_agent/query/metrics.py` | `5-28` 四条指标 |
| `[01]/backend/src/cs_agent/query/validator.py` | `12-64` 节点白名单；`65-86` 字面量枚举；`100-109` 时间窗；`112-150` 语句/规模/字面量/占位符；`151-169` 解析与聚合粒度；`170-182` join；`183-207` 输出列与行数 |
| `[01]/backend/src/cs_agent/query/executor.py` | `20-30` 引擎与超时；`34-53` 身份/参数/预算；`57-67` 成本与行数；`85-92` 返回体；`98-117` RLS 变量 |
| `[01]/backend/src/cs_agent/query/service.py` | `33-43` 五个操作；`57-64` 范围；`193-238` `orders.summary`；`287-355` 钱包 |
| `[01]/backend/migrations/versions/0011_controlled_query.py` | `15-20` 公共 RLS 表达式；`21-30` 列级 GRANT；`34-37` owner/role 作用域；`47-59` 视图定义与 `security_invoker` |
| `[01]/backend/src/cs_agent/db/business_tables.py` | `31-32` 列默认可空；`35-60` 业务表公共列；`302-345` 订单；`347-380` 订单行；`418-436` 支付；`438-500` 发货 |
| `[01]/backend/src/cs_agent/db/aftersales_tables.py` | `125-147` `refund_execution`（含 `refunded_at`） |
| `[01]/backend/src/cs_agent/aftersales/refunds.py` | `112-146` 退款落库与订单累计 |
| `[01]/backend/src/cs_agent/cancellation/service.py` | `556-565` 取消即退款 |
| `[01]/backend/src/cs_agent/commerce/checkout.py` | `533-555` 建单 `created_at`；`712-716` 行支付额；`718-739` 支付时点与状态 |
| `[01]/backend/tests/unit/test_sql_validator.py` | `14-36` 拒绝用例；`43-51` 官方聚合写法 |
| `[01]/contracts/sql_generation.json`／`support_summary.json` | 契约版本与状态 |
| `[01]/docs/02-design/跨项目契约草案.md` | `89-116` 样例请求/响应（含已漂移字段与 `kind` 枚举） |
| `[02]/backend/src/ops_agent/domain.py` | `20-21` 精度；`118-121` 金额量化；`136-248` 快照校验；`251-308` 指标；`590` 版本常量 |
| `[02]/backend/src/ops_agent/service.py` | `33-40` 时间解析；`99-106` 修订冲突；`131-143` 成本脱敏；`145-202` dashboard；`203-206` 上海日桶；`228-234` 成本权限 |
| `[02]/backend/src/ops_agent/auth.py` | `13-18` 角色能力（含 `cost.read`） |
| `[02]/backend/src/ops_agent/app.py` | `167-171` dashboard 路由 |
| `[02]/backend/src/ops_agent/db.py` | `22-27` 快照表主键 |
| `[02]/contracts/snapshot-v0.2.json` | `skus/orders/refunds` 仅声明数组 |
| `[02]/docs/01-requirements/PRD.md` | `210-222` 口径效力与 BR-OPS-001..007；`226-236` 指标字典；`288` 退款样例；`302-332` 数据清单与逻辑资源草案；`344-353` 异常表 |
