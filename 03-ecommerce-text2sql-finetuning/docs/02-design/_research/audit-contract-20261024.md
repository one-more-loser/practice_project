# 01/02/03 契约差异审计（2026-10-24）

| 项 | 内容 |
|---|---|
| 状态 | 只读审计记录；**不是联合契约、不是验收结论、不是批准** |
| 审计对象 | 03 `docs/02-design/三方契约差异表.md` v0.1（20 项）；`docs/01-requirements/PRD.md` v0.4；`docs/02-design/数据适配方案.md` v0.2；`需求追踪与验收矩阵.md` v0.3；`docs/03-data/01-小闭环计划.md` v0.2；`docs/02-design/质量验收方案.md` v0.2 |
| 外部依据 | 01 `CS03-CONTRACT-REVIEW-20261024-v1`（`01-ecommerce-customer-service-agent/docs/04-integration/03-query-contract-review-20261024/01对03查询契约评审答复.md`）；02 `OPS03-REVIEW-20261024-r1`（`02-ecommerce-operations-agent/docs/04-integration/OPS03-REVIEW-20261024/给03的契约评审答复.md`） |
| 方法 | 只读核对；每条给 01/02 原文章节行号或 03 文件行号；建议不写成已批准 |
| 结论 | 差异表 20 项覆盖了 01/02 的主要 headline 项，但缺 6 类关键细节；03 正文有 5 处与 01/02 答复不一致。共 12 条：P0 1、P1 7、P2 4 |

## 1. 摘要

- **覆盖判断**：差异表已覆盖 01 的 52 列、四指标、退款三 scope、参数链、UTC、RLS、80+40，以及 02 的四投影、8 指标、快照三元组、as_of、成本、零额退款、协议（C-02/C-04/C-05/C-06/C-07/C-15/C-16/C-17/C-18/C-19/C-20 及 §2–§4）。但只覆盖“结论”，未覆盖若干会直接影响 SQL 生成/标签冻结的**规则细节**（函数白名单、20 个全局字面量、LIMIT/截断、01 as_of 语义、01 错误码/retryable）。
- **状态标注**：差异表整体克制，C-02/C-08/§4 明确“02 待实现”，C-19 明确“不是 01/02 等价”，头部 L5 明确“不是三方联合契约”；但 C-16“已确认边界”、C-04“02 语义可用”措辞偏强（见 P2-10）。
- **03 正文错误**：PRD §4.4 仍 50 列；PRD §4.2 仍是旧信封；PRD §4.5/数据方案 §5.2 仍列 9 资源；PRD/数据方案/质量验收方案/评审记录/小闭环把 18/4/2 写成“按 01 意见”；数据方案 §13.1 仍把 01 已答复甚至已否定的问题列为未决。
- **沙箱证据**：24/24 用例（21 query+3 unsupported）与 11/12 安全用例有结果文件支撑；但小闭环 L77 的“10/11”与结果文件口径不一致，且 `JOIN ... ON TRUE` 未拦截这一已知静态缺口未进差异表（P2-12）。

## 2. 问题表（按严重度）

| # | 严重度 | 位置 | 现状 | 应为 | 依据 |
|---|---|---|---|---|---|
| P0-1 | P0 | `PRD.md:354`；`数据适配方案.md:47`；`质量验收方案.md:18`；`需求评审记录.md:308`；`01-小闭环计划.md:54`；`需求追踪与验收矩阵.md:96` | 五处写“按 01 逐题意见修订为 **18 query/4 clarify/2 unsupported**”。01 原文是“原表 19 个明确 query、1 个 query/unsupported、2 clarify、2 unsupported/error，**不是 18/2/4**；按规范化题面并替换 S08，可形成 **19 query、3 clarify、2 unsupported**；这是修订建议，不是已执行通过数”。18/4/2 实际是 03 保留 S06 clarify 后的自有选择，被误写成 01 意见；矩阵 AC-SQL-016 又以“01 答复与 03 标签一致”为验收标准，自相矛盾 | 改写为“01 建议 19/3/2；03 保留 S06 clarify、S05 query，故 18/4/2；差异待 01 复核”；AC-SQL-016 不得标为已满足 | 01 答复 L351；PRD L354；数据方案 L47；质量验收方案 L18；评审记录 L308；小闭环 L54；矩阵 L96 |
| P1-2 | P1 | `PRD.md:146`（§4.4）vs `PRD.md:338`（§12.1） | §4.4 仍写 `cs_order_item_self`(9)、`cs_shipment_self`(6)（合计 50 列）；§12.1 已改 52 列（订单行 10、发货单 7）。01 明确指出“03 PRD §4.1/4.4 的 50 列错误” | §4.4 改 10/7，或加“以 §12.1 与 01 答复为准”；全库搜索 50 列残留 | 01 答复 L22–L35；PRD L338 |
| P1-3 | P1 | `PRD.md:84-97`（§4.2）；`三方契约差异表.md:27`（C-13）、`:76-89`（§5） | PRD §4.2 输入信封仍是 SQL-MODEL-v0.1：`request_id/profile/question/locale/schema_revision/metric_revision/parameters`，无 `snapshot_ref/context/limits/schema_digest`；差异表 C-13 只写“三层映射”，未落 01 具体错误码与 retryable 语义 | §4.2 同步 §5 骨架（至少加 `snapshot_ref{snapshot_id,revision,digest,evaluation_as_of}`、`context{now,timezone,resolved_slots}`、`limits`、`schema_digest`）；C-13 记录 01 实际码：503 `model_unconfigured`/`model_protocol_error`、422 `schema_mismatch`、403 非 CUSTOMER、503 `query_unavailable`（不拆 DB 原因），`retryable` 仅 `unavailable`/`timeout` 为 true；网关对 429/502/503/504 有界重试 | 01 答复 L225–L250、L283；02 答复 L172–L180；差异表 L76–L89 |
| P1-4 | P1 | `三方契约差异表.md:22`（C-08）、`:25`（C-11）；`PRD.md:148`；`数据适配方案.md:94` | 差异表只写“validator 无逐列枚举校验”“`row_limit` 删除”；未记录 01 的 20 个**全局**字符串字面量白名单（日期/ID/商品名不能硬写）、函数白名单（仅 COUNT/SUM/MIN/MAX/AVG/COALESCE/DATE_TRUNC day\|week\|month）、拒绝节点（CASE/CAST/NULLIF/ROUND/LIKE/窗口/FILTER/AT TIME ZONE/INTERVAL）、LIMIT 字面量 1..50（缺省 20、>50 截、<1 拒、OFFSET 0..10000）、执行器响应无 `row_count/has_more/truncated` | C-08 增“20 全局字面量 + 函数白名单 + 拒绝节点；validator 不逐列校验枚举（`trade_status='topup'` 也能过 AST），语义校验归 03”；C-11 增“LIMIT 只能字面量；截断无标记，不能断言 `has_more`”；PRD §4.4/数据方案 §4.2 同步 | 01 答复 L46、L147–L162、L181、L273 |
| P1-5 | P1 | `三方契约差异表.md:29`（C-15）、`:76-89`（§5 `snapshot_ref.evaluation_as_of`） | C-15 只写 02 的 as_of/快照三元组，未写 01 侧语义：`SQLExecutor.as_of` 是调用方 `now`，不是数据库快照号/退款截止；`cs_product_public.as_of` 是 `sku.updated_at`；无 REPEATABLE READ | C-15 增 01 行：01 无历史快照，`evaluation_as_of` 只能当“参考时钟”；商品视图 `as_of` 同名不同义；01 标签须冻结参考时钟+fixture，不能声称按 as_of 重放 | 01 答复 L203–L224 |
| P1-6 | P1 | `PRD.md:156`（§4.5）；`数据适配方案.md:149-159`（§5.2） | PRD §4.5 仍列 9 个逻辑资源（含 `ops_payment/ops_cost/ops_inventory/ops_sales_period/ops_traffic_period`）；数据方案 §5.2 仍列 9 个“候选逻辑资源”，未标撤回。与 §12.3/差异表“只保留四投影、其余暂不建/暂不入”矛盾 | 两处收窄为四投影；其余标“已撤回/暂不建/暂不入”，并注明 02 答复 L65–L75 的“待实现/待共同确认” | 02 答复 L65–L75；PRD L360；差异表 L16/L22/L69 |
| P1-7 | P1 | `数据适配方案.md:283-285`、`:319`（§8、§8.3） | 02 样本版本仍写 `OPS-QUERY-v0.1`/`OPS-METRIC-v0.1`，与差异表 C-03 的 `OPS-QUERY-v0.2-draft.1`、02 的 `OPS-METRIC-v0.1-review.1` 不符；样本字段无 `snapshot_id/revision/digest/evaluation_as_of/visible_at`（1A L49/C-15 要求绑三元组）；§8.3 02 样例把 `store_id` 放进 `parameters`，`gold_sql` 用 `CASE WHEN` | 版本改 `OPS-QUERY-v0.2-draft.1`、`OPS-METRIC-v0.1-review.1`（注明记录非部署）；补快照三元组+`visible_at`；`store_id` 移出模型参数（身份/店铺服务端绑定）；02 金标不用 CASE（01 明确拒绝，02 无 validator 不能预设允许） | 02 答复 L51–L53、L61、L65–L75、L98–L106；PRD L101；差异表 L17/L29 |
| P1-8 | P1 | `数据适配方案.md:414-418`（§13.1） | Q-01“不补退款事件视图则 01 无法精确回答本期退款/退款率”仍列未决，但 01 已给钱包 `kind='refund'` 按 `occurred_at` 查询方案（过 validator），并说不能一律 unsupported；Q-02“orders.count 是否改含未付”已被 01 明确否定；Q-04 时区、Q-05 as_of 也已在 01 答复中回答 | 标“01 已答复”；Q-01 改为“订单级退款事件缺失（钱包到账面可用）”；Q-02 标“01 否定，不改”；Q-04/Q-05 改“已答复，待联合冻结” | 01 答复 L74–L95、L114–L135、L189–L199、L203–L224 |
| P2-9 | P2 | `三方契约差异表.md:34`（C-20）；`需求追踪与验收矩阵.md:114` | C-20 写“01 80 题+40 反例归属未定”，未标来源；01 答复全文无“80 题+40 反例”内容，未回答 Q-01-11。来源实为 01 契约草案 §3.3（经 03 评审包转述）；矩阵 L114“已收 01 答复”易被读成已讨论 | C-20 注明“来源：01 契约草案 §3.3；01 答复未答 Q-01-11，归属/环境/签署仍空”；矩阵同步 | `需求评审记录.md:247`；`01契约对齐评审包.md:40`、`:183`；01 答复全文 |
| P2-10 | P2 | `三方契约差异表.md:30`（C-16）、`:18`（C-04） | C-16 状态“已确认边界”易读成三方确认；C-04 写“02 语义可用、版本待部署”，但 02 明确 `OPS-METRIC-v0.1-review.1` 是当前实现记录、不是部署响应，且 SQL 面为 0 | C-16 改“02 评审接受（单方），未联合批准”；C-04 改“02 离线函数语义可参考，未部署” | 02 答复 L21–L26、L81、L94；差异表 L5 |
| P2-11 | P2 | `数据适配方案.md:112`（§4.3） | 仍写“01 没有退款事件视图……‘本期退款/某月退款’在 01 口径下无法精确表达：先 clarify”，与 01 的钱包到账面方案及“不能把所有退款问题标 unsupported”矛盾；1A L45 虽已修订，正文未标“已被 1A 覆盖” | 改为“订单级退款事件缺失；钱包到账面可按 `occurred_at` 查询；订单累计/钱包到账/02 现金期分 scope；退款率先 clarify” | 01 答复 L114–L135；数据方案 L45 |
| P2-12 | P2 | `01-小闭环计划.md:77`；`三方契约差异表.md:33`（C-19）；`质量验收方案.md:22`；`sandbox/results/miniloop-run-latest.json` | 小闭环 L77 写“安全 10/11”（11 个 validator 探针，含 `JOIN ... ON TRUE` 未拦）；结果文件 `summary.security_total=12`、`security_pass=11`，差异表 C-19 与质量验收方案 L22 均写 11/12。C-19 未披露 ON TRUE 静态缺口，也未列 01 要求第 5/6 条（冻结 fixture/参考时钟/统计；模型进程无凭据） | 统一口径：“结果文件 12 例安全用例 11 通过（RLS-01 异常）；另 validator 探针 10/11，`JOIN ... ON TRUE` 待修”；C-19 补 ON TRUE 与第 5/6 条 | 01 答复 L375–L380；结果文件 JSON；小闭环 L77；差异表 L33 |

## 3. 缺失项清单（需补进差异表/PRD/数据方案）

按归属分组，优先级对应上表：

| 归属 | 缺失项 | 补入位置 | 优先级 |
|---|---|---|---|
| 01 | validator 函数/节点白名单 + 20 个全局字面量 + “不逐列校验枚举→03 语义校验” | 差异表 C-08；PRD §4.4；数据方案 §4.2 | P1（P1-4） |
| 01 | LIMIT 字面量 1..50/缺省 20/截断无标记；执行器响应无 `row_count/has_more/truncated` | 差异表 C-11/C-13；数据方案 §4.2 | P1（P1-4） |
| 01 | `as_of`=调用方 now、非快照；商品 `as_of`=`sku.updated_at`；无 REPEATABLE READ | 差异表 C-15；PRD §12.3；数据方案 §6 | P1（P1-5） |
| 01 | 错误码分层与 retryable 语义（503/422/403、`query_unavailable`、有界重试） | 差异表 C-13；PRD §4.3 | P1（P1-3） |
| 01 | 80+40 来源（契约草案 §3.3）与“Q-01-11 未答” | 差异表 C-20；矩阵 §6 | P2（P2-9） |
| 02 | `OPS-QUERY-v0.2-draft.1`/`OPS-METRIC-v0.1-review.1` 版本号；样本绑三元组+`visible_at` | 数据方案 §8；差异表 C-03/C-15 | P1（P1-7） |
| 02 | `store_id` 不入模型参数；02 金标不预设 CASE | 数据方案 §8.3；PRD §4.2 | P1（P1-7） |
| 02 | 九资源撤回标记（`ops_payment/ops_cost/ops_inventory/ops_sales_period/ops_traffic_period`） | PRD §4.5；数据方案 §5.2 | P1（P1-6） |
| 三方 | 24 题覆盖数 19/3/2（01 建议）vs 18/4/2（03 选择）的差异记录 | PRD §12.2；数据方案 1A；质量验收方案 §0A；矩阵 AC-SQL-016 | P0（P0-1） |
| 三方 | 沙箱证据口径（12 例 11 过 vs validator 探针 10/11；ON TRUE；冻结时钟/无凭据） | 差异表 C-19；小闭环 §4；质量验收方案 §0A | P2（P2-12） |
| 三方 | 01 已答复项的状态回收（Q-01/Q-02/Q-04/Q-05） | 数据方案 §13.1 | P1（P1-8） |

## 4. 核对通过（无问题项）

- **52 列/六视图**：PRD §12.1 L338、数据方案 1A L43、质量验收方案 §0A L15 均写 52 列（订单行 10/发货单 7），与 01 答复 L22–L35 一致；`cs_product_public` 未被写成游客 profile。
- **四指标**：`created_at` 圈 4 状态且 `paid_minor>0`、`orders.count` distinct、净额非负、空集合 0/"0" 的规则在 PRD L339–L340、数据方案 L44/L105–L108、质量验收方案 L15/L109 与 01 L74–L112 一致；01 负净额 fixture 已删（数据方案 L45、质量验收方案 L18）。
- **退款三 scope 与 S06/S23**：PRD L341/L350–L353、数据方案 L45、质量验收方案 L16、差异表 C-05 均写“订单累计/钱包到账/02 现金期分开、退款率先 clarify、不一律 unsupported”，与 01 L114–L137、L345–L348 一致。
- **02 四投影状态**：PRD L360、数据方案 L48、质量验收方案 L19、差异表 C-02/C-08/§4 L69 均写“四投影待实现/待批准、不冻结七资源、`ops_payment/ops_cost/ops_inventory` 暂不建”，与 02 L65–L75 一致；未把四投影写成现状。
- **02 零额退款**：PRD L364、数据方案 L50、质量验收方案 L20/L128、差异表 C-17 均写“成功零额事件当前计入、待业务确认、不自行改标签”，与 02 L88–L92、L196 一致。
- **02 成本权限**：PRD L363、数据方案 L50、质量验收方案 L20、差异表 C-16 均写“`cost.read` 由 02 投影、无权限删字段、有权限且成本齐全也不返回实际利润”，与 02 L128–L136 一致。
- **02 快照/as_of**：PRD L362、数据方案 L49、差异表 C-15/§4 L68 均写“三元组、dashboard 无 as_of/revision、snapshot_id 取最高修订不是最新 watermark、不称任意截止重放”，与 02 L98–L106 一致。
- **01 调用链缺口**：差异表 §3 L54–L59 的六项（只绑 start/end、固定 30 天、无 request_id/context、`parameters={}`、无结构化 clarify、Graph 无 `orders.summary`）与 01 L233–L283 一致；C-06/C-12 未把缺口写成模型能力。
- **沙箱边界**：差异表 C-19 L33、数据方案 L52、质量验收方案 L9、小闭环 L79 均明确“不是 01/02 等价、不是金标”；RLS 异常不写金标（C-18 L32、质量验收方案 L114）。
- **契约性质声明**：差异表 L5、PRD L13、数据方案 L54 均明确“01/02 答复是评审意见，不是三方联合契约”；未发现把 01 单方答复写成联合契约。

## 5. 边界声明

- 本文是 03 侧只读审计记录，**不是联合契约，不是验收报告，不是训练/接入批准**；01/02 未审阅本文，文中的“应为”均为待协调建议。
- P0/P1 未处理前，不应冻结联合契约、不应把 24 题/沙箱结果升级为业务金标、不应据此启动训练或 01/02 接入。
- 审计未修改 01/02 任何文件，未启动服务/连库/训练/调用模型；沙箱数字仅引用 03 既有 `sandbox/results/` 证据。
