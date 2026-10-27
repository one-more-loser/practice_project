# 电商 Text-to-SQL 模型能力 PRD（v0.4 草案）

| 项 | 内容 |
|---|---|
| 文档状态 | 草案；需求规格待评审；不构成任何接口、数据或训练批准 |
| 版本 | v0.4（合并 01/02 评审答复：52 列、四指标口径、退款分层、01 调用链缺口、02 四投影/快照版本、协议映射、03 独立 PG 最小闭环；2026-10-24 13:30） |
| 日期 | 2026-10-24 13:30 +08:00 |
| 作者 | dsh（执行） |
| 决策人 | 用户 |
| 关联 | 01/02 R1 签收；《项目实战》4.2；01 评审答复 `CS03-CONTRACT-REVIEW-20261024-v1`；02 评审答复 `OPS03-REVIEW-20261024-r1`；01 `XCS-SQL-v0.1` 草案；02 `OPS-QUERY/METRIC v0.1` 草案；SQL-R1 / XiYan-SQLTraining 调研 |
| 变更 | v0.1：现场核对 + 第 1 轮访谈；v0.2：角色名统一；v0.3：Q1–Q3 决策落文；12:11 补 D-15..D-17；v0.4：01/02 答复落文，修正 52 列/指标/退款/02 投影/协议差异，新增 03 独立 PG 8 题最小闭环与 RLS 异常证据（D-18..D-26） |

> v0.4 把 01/02 的**评审意见**写成 03 需求修订，但仍不是联合契约：01 的 `XCS-SQL-v0.1` 未联合批准，02 的 `OPS-QUERY/METRIC` 未冻结，03 不据单方答复冻结训练标签。实际租卡、下载 8B、训练和付费调用仍待单独确认。

## 1. 背景

01 客服 Agent 与 02 运营 Agent 的初始业务版本已完成并有 2026-10-24 R1 独立签收。03 负责为两者提供 Text-to-SQL 模型能力，但不接管身份、权限、业务记录、业务计算、SQL 执行或写操作。

当前事实：

- 01 已实现 `customer_self/public` 的 6 个只读逻辑视图、指标目录、validator 与 executor；`XCS-SQL-v0.1` 仍是 01 侧草案，未联合批准；当前 SQL 模型是 deepseek-flash 临时方案，03 未接入。
- 02 的 FR-OPS-019 为 P1；经营 Schema/指标仍是 `OPS-QUERY-v0.1`/`OPS-METRIC-v0.1` 草案；02 现为固定工具 + 合成快照，没有自然语言 SQL 执行面；DEP-03 明确 03 当前无协议。
- 03 是绿地：此前只有 README，无需求、数据、代码或训练产物。
- 数据未准备：未找到原始业务 CSV 或字段字典；01/02 只有少量合成 JSON/演示快照。业务 CSV 不等于问句—SQL 监督标签，自动生成 SQL 也不等于金标。
- 底座 `Qwen/Qwen3-8B-Base` 未下载；本地只有 RTX 3060 Laptop 6GB，未安装 torch；AutoDL 未租用。

### 1.1 三类模型必须分开

| 类别 | 本项目中的含义 | 当前状态 |
|---|---|---|
| dsh 驱动模型 | 运行本会话的模型，负责访谈、设计、编码与文档 | 运行上下文声明为 `deepseek-v4.1-flash`；不是训练对象 |
| 训练底座 | 官方 `Qwen/Qwen3-8B-Base` | 未下载；后续锁定权重修订、tokenizer、许可证 |
| 外部生成/对照模型 API（EXTERNAL-GEN） | 可选的外部强模型 API，角色是“数据候选生成/同义改写”或“冻结测试上的对照基线”；**不承担知识蒸馏角色，不产生软标签，不参与训练损失，不作为奖励模型** | 未指定供应商、未批准预算、未调用；不得默认使用；若沿用 01 的 deepseek-flash，遵守既有调用限制，不复制/暴露密钥 |

> 说明：01 的 `deepseek-flash` 业务 API 只是“外部生成/对照模型 API（EXTERNAL-GEN）”的**可能供应商之一**，既不是 dsh 驱动模型，也不是训练底座；是否复用、用多少、花多少，另行批准。当前无密钥读取、无调用、无费用。

## 2. 目标与非目标

### 2.1 目标

- **G1 双消费面**：同一模型服务可按 profile 处理 `customer_self` 与 `operations_store`，输入为问题 + 获准 Schema + 指标定义 + 参数约束；`cs_product_public` 是 `customer_self` 内的公开商品视图，不是已实现的独立游客 SQL profile。
- **G2 受控输出**：输出 `query`（只读 SQL 候选）、`clarify`、`unsupported`、`error` 四类之一；query 必须能通过业务侧 validator 与隔离沙箱，语义正确。
- **G3 可复现路线**：完成同底座 Base 基线、SFT、GRPO，使用同一冻结评测协议记录增益与退化；保留 SFT 回退。
- **G4 可接入服务**：交付可配置的推理服务、协议样例、01/02 接入说明与回退方案；模型只返回候选，不执行 SQL。
- **G5 可核验证据**：数据、配置、日志、命令、版本、费用与已验/未验边界可被独立验收方复核。

### 2.2 非目标

- 不做用户身份、角色、店铺范围、字段可见范围的判定；不凭模型生成的 `WHERE` 充当鉴权。
- 不持有业务库凭据；不执行订单、退款、活动发布、改价等写操作。
- 不修改 01/02 的代码、数据库、运行环境、演示数据、可信时钟、备份或签收索引。
- 不承诺生产 SLA、长时高并发稳定性或完整浏览器/移动端能力；这些按后续阶段评审。
- 不用 EXTERNAL-GEN API 结果冒充 Base 基线；不用训练 reward 冒充测试准确率；不自宣验收通过。

### 2.3 v0.3 新增约束（用户 2026-10-24 11:31 确认）

- **参考项目适配，不是照搬**：参考 SQL-R1 的 RL 训练路线与 XiYan-SQLTraining 的数据处理/SFT/评测流程；模型适配、依赖、配置、模板、SQL 方言、Schema 表示、validator、指标口径和评测必须按本项目更新。旧版本可参考，但不能直接替换表名冒充业务适配。
- **数据三层**：公开数据筛选（Codex 负责）→ 业务数据适配（01/02 契约）→ 缺口生成（03 生成候选，交 Codex 独立检验）。公开数据只补通用 SQL 能力；业务能力必须用匹配自身契约的数据训练和检验。
- **小闭环先行**：20–30 题跑通“问题→SQL→安全检查→沙箱执行→业务语义”；后续训练规模按覆盖、质量与算力再定，不先堆标签不可靠的数据。
- **01 先行、02 分阶段**：同一模型服务保留两个 profile 目标；先以 01 已实现查询面验证闭环；02 同步准备规范与候选样本，待经营 Schema、指标与执行契约确认后，再纳入正式业务训练和接入验收。
- **口径隔离**：01 当前按 `created_at` 圈订单后统计实付/退款；02 草案按支付 `paid_at`、退款 `occurred_at` 各自事件时间统计。样本必须显式绑定 profile、Schema/指标修订和口径，不得混用。
- **显卡选型**：按最终任务量核对 AutoDL 市场，给首选与备选，说明显存、训练时间、当前单价、总费用、估算依据与待实测项；实际租卡与训练待方案及费用确认。

## 3. 角色与边界

| 角色 | 负责 | 不负责 |
|---|---|---|
| 03（本项目） | 需求/数据规范、数据集与标准 SQL、正确性校验、Base/SFT/GRPO、独立评测、推理 API、接入文档、可复现证据 | 身份/权限、权威业务记录、业务计算、SQL 执行、写操作、01/02 签收 |
| 01 客服 | 用户身份与角色、本人/店铺/字段可见范围、订单/余额/活动权威记录、受控执行、写操作确认与审计 | 训练模型、决定模型内部实现 |
| 02 运营 | 经营身份与店铺范围、指标口径、经营数据、受控执行、`cost.read` 等敏感权限 | 训练模型、把未批准口径固化进权重 |
| 验收方（用户/Codex） | 独立留出、追加反例、最终验收与评分 | 代替 03 做训练或数据建设 |

## 4. 消费面与契约（提议，未批准）

### 4.1 Profile

| profile | 消费方 | 逻辑 Schema | 指标修订 | 当前状态 |
|---|---|---|---|---|
| `customer_self` | 01 | `CS-QUERY-B2-v1`（6 视图/**52 列**） | `CS-METRIC-B2-v1` | 已实现，契约草案；`cs_product_public` 是公开商品字段，不等于游客独立 profile |
| `operations_store` | 02 | 四个待实现投影：`ops_order`、`ops_order_item`、`ops_refund`、`ops_sku`（`OPS-QUERY-v0.2-draft.1`） | `OPS-METRIC-v0.1-review.1`（当前实现记录，非部署响应） | 当前真实存储是 `ops_snapshots.body JSON`；无 SQL 执行面，全部“待实现/待批准” |

### 4.2 输入信封（SQL-MODEL-v0.2 提议）

```json
{
  "contract_version": "SQL-MODEL-v0.2",
  "request_id": "req-...",
  "profile": "customer_self",
  "question": "我上个月买了几单？",
  "locale": "zh-CN",
  "context": {
    "now": "2026-10-24T04:36:00Z",
    "timezone": "Asia/Shanghai",
    "resolved_slots": {
      "start_at": "2026-09-01T00:00:00+08:00",
      "end_at": "2026-10-01T00:00:00+08:00"
    }
  },
  "schema_revision": "CS-QUERY-B2-v1",
  "schema_digest": "<sha256>",
  "metric_revision": "CS-METRIC-B2-v1",
  "parameters": {"start_at": "timestamptz", "end_at": "timestamptz"},
  "limits": {"max_rows": 50, "read_only": true}
}
```

约束：

- 不含 `user_id`、`store_id`、`role`、`tenant`、数据库凭据或任何身份/权限声明；身份与范围由业务端在服务端绑定。`context.resolved_slots` 只放已确认的业务槽位，不放身份。
- `parameters` 是“名字→类型/枚举”表，不是值；值放 `context.resolved_slots` 或调用方受控绑定，模型不得自行扩大范围。
- `schema_revision`/`schema_digest`/`metric_revision` 必须与 profile 匹配；未知或不匹配时返回 `unsupported`/`error`，不得猜。
- 02 的 `snapshot_id`/`as_of` 属 `context` 可选字段；不能同时缺省，也不能用 `as_of` 冒充精确 revision 重放。
- 问题文本按业务脱敏规则传入；03 不接收完整个人数据或未脱敏会话。

### 4.3 输出信封（SQL-MODEL-v0.2 提议）

```json
{
  "request_id": "req-...",
  "kind": "query",
  "profile": "customer_self",
  "schema_revision": "CS-QUERY-B2-v1",
  "schema_digest": "<sha256>",
  "metric_revision": "CS-METRIC-B2-v1",
  "sql": "SELECT ... FROM ... WHERE ... :start_at ... :end_at ...",
  "parameter_names": ["start_at", "end_at"],
  "clarify": null,
  "unsupported": null,
  "error": null,
  "service": {
    "model_identifier": "qwen3-8b-sql",
    "revision": "<immutable-service-revision>"
  }
}
```

四类互斥：

| kind | 必填 | 禁止 | 说明 |
|---|---|---|---|
| `query` | `sql`、`parameter_names` | 空 SQL、多语句、写语句 | 只读 SELECT 候选；参数名不含冒号 |
| `clarify` | `clarify.slots[]`（槽位名、追问、可选值） | `sql` | 缺条件/歧义时使用；不得静默猜 |
| `unsupported` | `unsupported.reason_code` | `sql` | 越界资源、无权限、写请求、未定义指标等 |
| `error` | `error.code`、可读消息 | `sql` | 仅契约/内部错误；不把用户歧义当 error |

通用规则：

- 回显 `schema_revision`、`schema_digest`、`metric_revision`；调用方必须校验，不得只信模型。
- `service.revision` 由部署绑定，模型不得猜自身身份；同一 revision 的行为应可复现。
- 错误消息不得包含密钥、DSN、完整 SQL 参数值、个人信息。
- 兼容映射：01 的 `XCS-SQL-v0.1` 使用 `kind/schema_revision/schema_digest/metric_revision/sql/parameter_names/service`；03 提议信封是超集，最终字段以联合评审为准。

### 4.4 01 侧硬约束（源码事实，2026-10-24）

- 视图：`cs_order_self`(14)、`cs_order_item_self`(10)、`cs_shipment_self`(7)、`cs_wallet_summary_self`(4)、`cs_wallet_entry_self`(8)、`cs_product_public`(9)。
- 参数：`start_at`、`end_at`、`order_id`、`sku_id`、`status`。
- validator：单条只读 SELECT；≤8192 字节；≤512 节点；≤3 join；≤2 CTE；depth ≤18；join 仅 `order_id/sku_id/shipment_id` 等值；字符串字面量必须在枚举白名单；数值 ≤1e9；除 `count(*)` 外禁 `*`；默认 limit 20、最大 50；列名不得含 `phone/address/street/password/secret/token/owner/staff/cost_` 等。
- executor：命名参数绑定；`start_at/end_at` 必须成对且带时区；窗口 ≤366 天；服务端设置 `search_path` 与身份会话变量；RLS 绑定范围；模型 `WHERE` 不是鉴权；`fetchmany(51)` 截断检测。
- 指标：`orders.count`（`trade_status IN ('paid','completed','cancelled_refunded','closed_refunded')` 且 `paid_minor>0`，排除未支付/未支付取消）、`orders.paid_minor`、`orders.refunded_minor`、`orders.net_paid_minor`；金额 CNY 分；`created_at` 半开窗；退款按订单级金额，不按退款条目重复计；`orders.refunded_minor` 是截至查询时刻的累计退款，不是“本期退款”。
- 注意：01 的 `date_range` 在调用方未给时间时默认“最近 30 天”；这是 01 固定操作层的行为，不等于 03 模型可以静默套默认。模型侧缺时间窗时是否澄清，见 Q3。

### 4.5 02 侧约束（PRD 草案事实）

- FR-OPS-019（P1）：受控经营 Text-to-SQL，输出候选/澄清/不支持/错误，由 02 受控执行并给出来源。
- 逻辑资源（02 答复后的四投影，全部待实现/待批准）：`ops_order`、`ops_order_item`、`ops_refund`、`ops_sku`；`ops_payment/ops_cost/ops_inventory/ops_sales_period/ops_traffic_period` 不在四投影，不生成其 SQL。
- 指标：`payment_minor`、`refund_minor`、`net_paid_minor`、`paid_order_count`、`cohort_refund_order_rate`、`cohort_refund_amount_rate`、`paid_units`、`average_order_minor`，以及 `channels[]` 分组（草案）。02 当前只有 8 个标量指标 + `channels[]` 的确定性函数，SQL 消费面为 0；`OPS-METRIC-v0.1` 不是响应字段。
- 规则：Asia/Shanghai；半开窗；支付按 `paid_at`、退款按 `occurred_at`；金额整数分；净实付可为负；退款不折数量；成本缺失时毛利未知，不得补 0；`cost.read` 由 02 控制；跨维度不可直接相加；权限不因模型输出扩大。
- 02 当前没有 03 可调用的执行接口；最终字段、维度、`as_of`、错误码待联合评审。

### 4.6 契约状态

| 项 | 状态 |
|---|---|
| 01 `XCS-SQL-v0.1` | 01 侧草案，`01_consumer_draft_not_jointly_approved`；03 不视为已批准 |
| 01 评审答复 | `CS03-CONTRACT-REVIEW-20261024-v1` 已收；是 01 评审意见，不是联合契约；03 已按 52 列/指标/调用链缺口修订，见 `docs/02-design/三方契约差异表.md` |
| 02 评审答复 | `OPS03-REVIEW-20261024-r1` 已收；四投影/执行面/as_of 均未实现；不是 SQL 冻结契约；03 不纳入正式训练/接入 |
| 01 对齐评审包 | `docs/02-design/01契约对齐评审包.md`（v0.1，已由用户转发并回收答复） |
| 02 对齐评审包 | `docs/02-design/02契约对齐评审包.md`（v0.1，已由用户转发并回收答复） |
| 03 `SQL-MODEL-v0.2` | 本文提议；协议映射见差异表 §2/§5；待 01/02/03 联合评审 |
| 参数绑定 | 01 直连执行器可绑 5 参数，但客服 `_sql_query` 只赋 `start_at/end_at`；缺口列 01 接入依赖 |
| 版本/摘要校验 | 01 已有 `schema_revision/digest`；02 需 `snapshot_id/revision/digest`；`service.revision` 由 03 服务端注入 |
| 错误码 | 本文提议；待统一 |

### 4.7 参考项目适配基线（提议）

| 参考 | 可复用 | 必须改造 | 不采用/风险 |
|---|---|---|---|
| SQL-R1 | RL 训练组织、rollout/奖励分层思路、执行反馈、组内诊断、评测记录 | 底座换 Qwen3-8B-Base；Schema/问题/SQL 序列化；PostgreSQL 方言；01/02 指标与 validator；奖励权重与沙箱接口 | 其底座/模板/数据分布与本项目不同；不能把其 SQL 文本或分数当本项目基线 |
| XiYan-SQLTraining | 数据处理流程、SFT 配置组织、评测脚本结构、Schema 表示经验 | 模型/模板/特殊 token；数据字段；SQL 方言；指标口径；许可证与依赖 | 不能只替换表名；其底座与训练框架假设需重验 |

- 两个参考项目的仓库 URL、commit、license、复用/修改文件清单记录在 `docs/02-design/参考项目复用与修改清单.md`；未核实项不得写成已适配。
- 参考项目代码只作方法与配置参考；不把其权重、数据或评测分数混入本项目 Base/SFT/GRPO 结果。

### 4.8 数据交付与 Codex 检验接口（提议）

Codex 负责公开数据调研筛选；03 只接收通过筛选的数据包，或在覆盖不足时生成缺口候选。交给 Codex 独立检验的数据包至少包含：

- 来源与版本：数据集名、URL/commit、license、抓取/转换时间、原始字段说明。
- 问题与 profile：原问题、规范化问题、profile、Schema/指标修订、参数约束。
- 期望响应：`query`/`clarify`/`unsupported`/`error`；标准 SQL 或澄清槽位；禁止项。
- 可重建测试数据：fixture 种子/快照、初始化命令、期望结果断言（排序/重复行/NULL/金额/时区/边界）。
- 审核与切分：来源类型（public/business/gap）、审核状态、模板/来源分组、train/dev/test 归属、同义改写关系。
- 复现：执行命令、环境版本、随机种子、结果指纹；缺项写“未采集”。

边界：公开数据不得冒充业务金标；业务适配样本必须匹配 01/02 获准逻辑视图、字段、类型、关联、方言和指标口径；03 生成候选不等于金标，Codex 检验也不替代 01/02 的业务签收。

## 5. 功能需求（FR-SQL）

| ID | 优先级 | 需求 | 验收要点 |
|---|---|---|---|
| FR-SQL-001 | P0 | 按 `profile` 路由到对应 Schema/指标；未知 profile 返回 `unsupported` | 交叉 profile 的 SQL 被拒绝；不混用 01/02 视图 |
| FR-SQL-002 | P0 | 绑定并回显 Schema/指标/服务修订；调用方校验 | 修订不匹配时失败关闭；响应含 digest/revision |
| FR-SQL-003 | P0 | 四类响应互斥、可解析、无多余 SQL | query 无空 SQL；clarify/unsupported/error 不含 SQL |
| FR-SQL-004 | P0 | 业务概念、别名、枚举、时间表达标准化到获准字段/值 | 枚举白名单；时区 Asia/Shanghai；半开窗；不发明列 |
| FR-SQL-005 | P0 | 按指标定义聚合；正确粒度；退款/净额/未知值口径 | 结果等价；不跨粒度重复求和；未知不补 0 |
| FR-SQL-006 | P0 | 缺必要条件或存在实质歧义时澄清 | 列出缺失槽位/选项；不猜；补全后可 query |
| FR-SQL-007 | P0 | 多表关联、过滤、分组、排序、limit | 仅获准 join 键；避免 fan-out；排序有确定性 tie-breaker |
| FR-SQL-008 | P0 | 只读安全与资源约束 | 单条 SELECT；无 DDL/DML；资源/行数/字段/函数受限 |
| FR-SQL-009 | P0 | 参数契约：名称、类型、约束、绑定 | 参数名在白名单；`start_at/end_at` 成对带时区；无自由文本注入 |
| FR-SQL-010 | P1 | 错误分类：模型错误/契约错误/执行器错误/环境故障 | 错误码稳定；不泄露敏感信息；可重试性明确 |
| FR-SQL-011 | P0 | 推理服务：地址、密钥引用、模型标识、服务修订、协议 | 健康检查；超时/并发/长度限制；结构化输出 |
| FR-SQL-012 | P0 | 冻结评测：Base/SFT/SFT+GRPO 同口径 | 同一测试集/执行器/奖励修订；记录增益与退化 |
| FR-SQL-013 | P1 | 01/02 接入与回退 | 适配差异、样例、回退到 SFT/临时方案；不自动切换 |
| FR-SQL-014 | P0 | 参考项目适配：SQL-R1 / XiYan-SQLTraining 的复用与修改可追溯 | 记录 URL/commit/license；逐项列出复用/改造/不采用；不以替换表名冒充适配 |
| FR-SQL-015 | P0 | 数据三层分离：公开数据筛选、业务数据适配、缺口生成 | 公开数据只补通用 SQL；业务金标绑定 01/02 契约；Codex 筛选/独立检验有记录 |
| FR-SQL-016 | P0 | 数据交付包可重建、可独立检验 | 含来源/版本、问题、profile、修订、参数、期望响应、标准 SQL/澄清、fixture、断言、切分 |
| FR-SQL-017 | P0 | 01/02 口径隔离 | 样本显式绑定 profile/指标修订；01 `created_at` 圈单与 02 事件时间不混用；交叉用例被拒 |
| FR-SQL-018 | P1 | 02 分阶段纳入 | 01 先行闭环；02 先规范/候选；正式训练待经营 Schema/指标/执行契约确认 |

## 6. 业务规则（BR-SQL）

| ID | 规则 | 可测试表述 |
|---|---|---|
| BR-SQL-001 | 身份与权限不在模型 | 模型输入无身份字段；SQL 的 `WHERE` 不被当作鉴权；越权候选被业务侧拒绝 |
| BR-SQL-002 | 只读、无凭据 | 仅 SELECT；无写语句/函数；模型与训练样本不含 DSN/密钥 |
| BR-SQL-003 | 时间窗 | Asia/Shanghai；`[start_at, end_at)`；仅日期时 end 取次日 00:00；无批准默认则不静默套 30 天 |
| BR-SQL-004 | 金额 | CNY 整数分；不用浮点；展示层才格式化；比较精确到分 |
| BR-SQL-005 | 退款/取消/净额 | 支付按支付时间、退款按退款发生时间；净实付=支付-退款；01 订单净额非负，02 现金期净额可为负；退款不折数量；队列口径按 02 定义 |
| BR-SQL-006 | 未知值 | 成本/退款分摊等未知时返回 unknown/null；不得补 0，不得发明公式 |
| BR-SQL-007 | 关联与去重 | 仅获准键；先按正确粒度聚合再关联；避免一对多 fan-out 重复计数 |
| BR-SQL-008 | 澄清优先 | 缺时间窗/粒度/指标/范围时澄清；仅当 profile 文档明确允许默认时才用默认 |
| BR-SQL-009 | 结果等价 | 多 SQL 可等价；按列名/类型、多重集、NULL=NULL、金额精确、时区/边界比较；仅问题要求时比较顺序 |
| BR-SQL-010 | 数据来源 | 记录来源、许可、转换、审核；未审核样本不得进金标；公开数据不冒充业务金标 |
| BR-SQL-011 | 参考项目适配 | 只复用方法与流程；模型/模板/方言/Schema/指标/validator 必须重验；保留 commit/license；不直接替换表名 |
| BR-SQL-012 | 数据分层 | public 只补通用 SQL；business 必须匹配 01/02 契约；gap 由 03 生成候选并交 Codex 检验；三者不得混为同一金标 |
| BR-SQL-013 | 口径隔离 | 01 订单指标按 `created_at` 圈订单后统计实付/退款；02 按 `paid_at`/`occurred_at` 事件时间；样本绑定 profile 与修订，禁止混算 |
| BR-SQL-014 | 默认值 | 只有 profile 契约明确允许时才默认；01 固定操作层 `date_range` 的最近 30 天不是模型可静默默认 |

## 7. 非功能需求（NFR-SQL）

| ID | 需求 | 验收要点 |
|---|---|---|
| NFR-SQL-001 | 可复现 | 固定源码/数据/模型/Schema/指标/执行器/奖励修订、种子、命令；重跑结果在容差内一致 |
| NFR-SQL-002 | 评测隔离 | 测试集冻结；沙箱不连 01/02 应用库；训练/奖励不得读测试答案 |
| NFR-SQL-003 | 性能/资源 | 记录 p50/p95 延迟、吞吐、显存、tokens/s；阈值待基线后定 |
| NFR-SQL-004 | 安全/日志 | 最小权限；日志不含密钥/DSN/完整个人信息；危险 SQL 100% 拦截 |
| NFR-SQL-005 | 版本/谱系 | Base/SFT/GRPO/合并/量化分开保存；哈希与父子关系可追溯；不覆盖原始权重 |
| NFR-SQL-006 | 可维护 | profile 配置化；Schema/指标修订可升级；回归用例随契约更新 |
| NFR-SQL-007 | 成本/资源可审计 | GPU 首选/备选、显存、单价、预计时长、总费用、估算依据与待实测项；实际租卡/训练需确认 |
| NFR-SQL-008 | 参考项目许可/版本 | 记录 URL/commit/license；复用文件与修改点可追溯；不引入不兼容许可证 |
| NFR-SQL-009 | 协作归属 | 子代理/外部生成/人工协助的任务、文件范围、产出与复核结论记录；主执行对最终结果负责 |

## 8. 数据需求（摘要，详见附录 A）

- **三层来源**：公开数据（Codex 调研筛选，只补通用 SQL 能力）→ 业务适配（匹配 01/02 获准视图、字段、类型、关联、方言、指标口径）→ 缺口生成（03 生成候选，交 Codex 独立检验）。三层分别标注来源、许可、转换与审核状态，不得混为同一“金标”。
- 覆盖 01/02 的问题族、逻辑资源、字段、枚举、关联、指标、金额单位、时间窗、退款/取消、未知值、空结果；01 与 02 的口径分别绑定，禁止混算。
- 先做 20–30 题小闭环（问题→SQL→安全检查→沙箱执行→业务语义），通过后再按覆盖、质量与算力扩量；不在标签不可靠时先堆量。
- 每个样本至少绑定：问题、profile、Schema/指标修订、参数约束、期望响应类型、标准 SQL 或澄清标签、fixture、结果校验规则、来源与版本、许可、审核状态、切分组、模板/同义改写关系。
- 交给 Codex 检验的数据包必须可重建：来源/版本、问题、profile、修订、参数、期望响应、标准 SQL 或澄清槽位、fixture 种子/快照、结果断言、执行命令、环境与随机种子；缺项写“未采集”。
- 按模板/问题族/相近 SQL/来源分组切分，再扩写问法；同义改写不得跨 train/dev/test；测试集冻结，GRPO prompt 只从 train 取。
- 一题可有多条等价 SQL；不能只靠字符串一致、可执行或结果非空判定；用多组有区分度 fixture，覆盖重复计数、空结果、NULL、金额、时区/边界、退款/取消、权限边界。
- GRPO 每题必须有可信、可重复的正确性依据（标准结果、业务断言、对照 fixture 或澄清标签）；没有唯一 SQL 文本不等于没有答案依据或验证器。
- 缺必要条件时澄清；默认值必须有 profile 契约的明确依据，01 固定操作层的最近 30 天默认不等于模型可静默默认。

## 9. 验收（AC-SQL，阈值待基线后定）

| ID | 验收项 | 方法 | 当前阈值 |
|---|---|---|---|
| AC-SQL-001 | 小闭环 | 20–30 题全链路：问题→SQL→安全→沙箱→语义 | 全部可复核；无绕过 |
| AC-SQL-002 | 数据 seed v0.1 | 来源/许可/审核/去重/切分/fixture 完整 | 100% 有来源与审核状态 |
| AC-SQL-003 | Base 基线 | 同底座、同测试、同执行器 | 记录，不设通过线 |
| AC-SQL-004 | SFT | 冻结测试结果等价率、执行有效率、澄清/不支持 | 待定（建议 ≥85% customer_self，≥80% operations_store，待基线校准） |
| AC-SQL-005 | GRPO | 与 SFT 同口径比较；增益/退化 | 待定；不得用 reward 上涨替代 |
| AC-SQL-006 | 安全 | 危险/越权/写请求/凭据泄漏 | 100% 拦截；0 泄漏 |
| AC-SQL-007 | 服务契约 | 四类响应、版本、参数、超时/并发、样例 | 01/02 适配样例通过 |
| AC-SQL-008 | 证据包 | 配置/日志/命令/费用/已验未验 | 可复现；缺项写“未采集” |
| AC-SQL-009 | 独立验收 | 验收方留出与反例 | 由验收方判定，03 不自签 |
| AC-SQL-010 | 参考项目适配 | SQL-R1/XiYan 复用/修改清单、commit/license、差异测试 | 清单可追溯；不把替换表名当适配 |
| AC-SQL-011 | 数据交付包 | 字段完整、fixture 可重建、Codex 可独立复跑 | 100% 来源/审核/切分可查；缺项写“未采集” |
| AC-SQL-012 | GPU 选型/费用 | 首选/备选、显存、单价、时长、总费用、假设与待实测；方案与费用确认后才租卡 |
| AC-SQL-013 | 01/02 口径隔离 | 样本绑定 profile/修订；交叉口径反例 | 01 `created_at` 与 02 事件时间不混用 |

## 10. 决策状态

| ID | 决策 | 结论/建议 | 状态 |
|---|---|---|---|
| D-01 | 数据来源与首版规模（Q1） | 公开数据筛选（Codex）+ 业务数据适配 + 缺口生成；Codex 先筛公开数据，覆盖不足时 03 生成候选交 Codex 独立检验；20–30 题小闭环，后续规模按覆盖/质量/算力定；公开数据只补通用 SQL，业务能力必须匹配自身契约 | **已确认**（2026-10-24 11:31） |
| D-02 | v1 覆盖与优先级（Q2） | 保留同一服务双 profile 目标；01 已实现查询面先行验证闭环；02 同步规范/候选，待经营 Schema、指标及执行契约确认后纳入正式训练与接入验收 | **已确认**（2026-10-24 11:31） |
| D-03 | 正确性判定与澄清（Q3） | 多组 fixture 结果等价 + 契约校验 + 响应类型联合判定；缺必要查询条件澄清；默认值须有契约依据；质量检查覆盖重复计数、空结果、NULL、金额、时间边界、退款、权限 | **已确认**（2026-10-24 11:31） |
| D-04 | 全参/LoRA/QLoRA | 24GB 先 QLoRA；48GB 可 LoRA bf16；全参 SFT 单卡 80GB 也需 ZeRO-3/offload，不作首选；GRPO 用 LoRA/QLoRA 策略 | 待数据长度与小试 |
| D-05 | GPU 选型与费用（首选/备选） | AutoDL 单卡；按最终任务量给首选/备选、显存、单价、时长、总费用、假设与待实测；实际租卡待方案与费用确认 | 待方案与费用确认 |
| D-06 | 框架/版本/上下文/超参 | 参考 SQL-R1/XiYan 流程，按 Qwen3-8B-Base 重验；版本锁定实测组合；上下文按数据长度定 | 待设计评审 |
| D-07 | GRPO 奖励/权重/参数 | 分层奖励；硬门禁+语义为主；权重/组大小/KL 待评审 | 待设计评审 |
| D-08 | 质量/性能阈值 | 先 Base 基线再定；分 profile/问题族报告 | 待基线 |
| D-09 | 01/02 最终 SQL 契约 | 以 XCS 为 01 兼容基线；02 扩展字段联合评审 | 待联合评审 |
| D-10 | EXTERNAL-GEN API 与预算 | 首轮不用；若用，另批预算、限流、许可与审核 | 待批 |
| D-11 | 参考项目复用/修改 | 复用 SQL-R1 的 RL 组织/奖励/评测思路与 XiYan 的数据处理/SFT/评测流程；模型、模板、方言、Schema、指标、validator、依赖必须改造并留差异记录 | 建议，待设计评审 |
| D-12 | 数据交付包与切分 | 字段完整、fixture 可重建、Codex 可独立复跑；按模板/来源/问题族分组，同义改写不跨集 | 建议，待评审 |
| D-13 | 01/02 口径隔离 | 01 按 `created_at` 圈订单后统计实付/退款；02 按 `paid_at`/`occurred_at` 事件时间；样本绑定 profile/修订，禁止混算 | **已确认**（2026-10-24 11:31） |
| D-14 | 子代理协作与文件边界 | 仅 03 内；子代理只写各自 `_research/*.md`；主执行独占最终文档并复核；01/02 只读；不训练/租卡/付费 | **已确认**（2026-10-24 11:33） |
| D-15 | 本轮需求评审基线 | PRD v0.3 + 矩阵 v0.2 + 评审记录 §10 + 四份方案草案；仅用于评审，不构成需求/设计通过 | **已确认**（2026-10-24 12:11） |
| D-16 | AutoDL 预算方法 | 先按 GPU 文档 §6.4 第三方参考价给区间，标来源/日期/假设；官方实时价未取得前不锁卡；正式选型补实时单价/库存/总费用 | **已确认**（2026-10-24 12:11） |
| D-17 | 01/02 并行准备 | 01 小闭环与 02 契约评审准备并行；02 先规范/候选/试点评测；正式训练待 02 契约确认；01/02 代码只读 | **已确认**（2026-10-24 12:11） |
| D-18 | 01 能力基线 | 6 视图 **52 列**；`cs_product_public` 是公开商品字段，不是游客独立 profile；首轮只适配 `customer_self` | 01 答复确认；待联合契约 |
| D-19 | 01 指标/退款口径 | 四指标按 `created_at` 圈 4 状态且 `paid_minor>0` 的订单；订单累计退款/钱包到账/付款发生额分开；订单净额非负；创建总数另标 | 01 答复确认；待标签冻结 |
| D-20 | 01 调用链缺口 | 只绑 `start_at/end_at`、固定 30 天、无结构化澄清是 01 改动项；不能用训练绕过，03 只验证“明确问题+可信参数” | 01 答复确认；待 01 排期 |
| D-21 | 02 资源状态 | 只保留四投影 `ops_order/ops_order_item/ops_refund/ops_sku`，全部“待实现/待批准”；撤回七资源可直接冻结 | 02 答复建议；待用户/02 确认 |
| D-22 | 02 快照/截止 | 当前 dashboard 无 `as_of/revision` 选择器；训练标签绑快照三元组+窗口+筛选+指标版本；不声称任意截止重放 | 02 答复确认；待 02 实现 |
| D-23 | 协议映射 | 03 用 `profile`+`SQL-MODEL-v0.2` 骨架；01 适配 `consumer_profile`/XCS 包络；02 采用新骨架；差异由适配层处理 | 03 拟采用；待三方冻结 |
| D-24 | 03 独立 PG 小闭环 | 16.2 沙箱；8 题×3 fixture；24/24 用例（21 query+3 unsupported）通过，安全 17/18；只验“问题+可信参数→SQL→校验→执行”，不声明 01/02 接入 | 已执行（沙箱证据） |
| D-25 | 01 RLS 异常 | 独立 PG 复现 inactive store 可见订单；`EXPLAIN` 显示 `s.store_id=s.store_id`；不作为金标，反馈 01 复核 | 待 01 复核 |
| D-26 | 02 成本/零额退款 | `cost.read` 由 02 投影；有权限且成本齐全也不返回实际利润；成功零额退款是否计入 cohort 退款订单数待 02 业务确认 | 待 02 确认；不自行改标签 |
| D-27 | S06 拆分 | S06C（clarify）与 S06Q（query）分开编号、分开统计；S06Q 执行通过不算澄清能力通过 | 用户 13:10 要求；已执行 |
| D-28 | 校验器对照 | 03 validator（sqlglot 27.20）与 01 validator（>=30.24）同批 26 例对照；3 例差异待联合决定；对照前只证明 03 目标级行为 | 用户 13:10 要求；见 `docs/02-design/校验器对照.md` |

## 11. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 无真实 CSV/字段字典 | 数据分布可能偏离真实业务 | 公开数据只补通用 SQL；业务适配样本绑定 01/02 契约并逐题审核；未来 CSV 增量接入；缺口生成交 Codex 检验 |
| 01 契约仍是草案 | 接入返工 | 先小闭环验证；契约联合评审后再扩训 |
| 02 无执行面 | 经营 profile 无法端到端验证 | 先做规范/试点评测；推动 02 提供受控执行沙箱 |
| 8B 单卡资源不足 | 训练慢/无法 GRPO | 先 QLoRA 小试；实测显存/吞吐；必要时调整卡型 |
| GRPO 奖励被利用 | 指标虚高、业务错误 | 分层奖励、反作弊用例、多 fixture、组内诊断、冻结测试 |
| 验收前反复调参 | 过拟合测试 | 测试集冻结；验收方留出与追加反例；不利用测试答案调参 |
| 参考项目底座/方言/模板不匹配 | 迁移错误、返工 | 逐项复用/修改清单；Qwen3-8B-Base/PostgreSQL/01-02 契约重验；不替换表名冒充适配 |
| 公开数据许可或分布不匹配 | 许可风险、负迁移 | Codex 筛选并记录 license；公开数据只补通用 SQL；业务金标自建并审核 |
| 01/02 口径混用 | 结果错误、训练污染 | 样本绑定 profile/指标修订；01 `created_at` 与 02 事件时间分列；交叉反例 |
| AutoDL 价格/库存波动 | 预算偏差 | 记录抓取时间、单价、备选；租前复核；不用估算代替实付 |
| 子代理并发或推断错误 | 文档失真 | 文件锁；主执行抽查证据；未核实项标“待核实” |

## 12. 评审答复后的修订（v0.4，2026-10-24）

> 本节是 03 对 01/02 单方答复的吸收，不是联合契约。差异表的现状/拟采用/待实现/未决四态见 `docs/02-design/三方契约差异表.md`。

### 12.1 01 能力与指标修正

| 项 | 修正后 |
|---|---|
| 字段数 | 6 视图 **52 列**（订单行 10、发货单 7），不是 50；`cs_product_public` 是公开商品字段，不是游客独立 SQL profile |
| 指标口径 | `orders.count/paid_minor/refunded_minor/net_paid_minor` 均按 `created_at` 圈定“支付成功”订单（4 状态 + `paid_minor>0`）；`orders.count` 是 distinct order，不是创建总单数；创建总单/付款发生额/钱包到账另标 scope |
| 退款/净额 | 订单累计退款 `refunded_minor`（含窗后）与钱包 refund `occurred_at` 到账面、02 现金期退款分开；01 订单净额非负；删除“负订单净额”正常样本；02 现金期净额可为负，不混用 |
| 退款率 | 不一律标 unsupported；按查询范围、公式与能力判断；先 clarify 口径（订单批次/支付 cohort 分子分母不同），有可信依据才 query |
| 参数 | 5 参数白名单；直连执行器可绑 `order_id` 等；客服 `_sql_query` 只赋 `start_at/end_at`、固定 30 天；这是 01 接入改动，不是训练能绕过 |
| 时间 | 01 会话 UTC；`DATE_TRUNC` 是 UTC 桶；上海自然日过滤可用上海起止，不能把 UTC 桶当上海桶；上海周/月桶列 01 待评审 |
| RLS | 01 答复主动提示活跃店策略 `EXISTS(... s.store_id=store_id ...)` 可能相关；03 独立 PG 已复现 inactive store 可见，反馈 01，不作金标 |

### 12.2 01 25 个分支逐题修订（S06 拆分）

01 对 S01–S24 给出建议，03 采纳的要点：

- S01/S02 分开“创建单数”和 `orders.count`（4 状态、`paid>0`）；S03 用 UTC 半开窗表达上海自然日过滤，query；S04 上海周桶 unsupported（不拿 UTC 桶冒充）。
- S05 用 `created_at` 窗口；S06 先 clarify“订单累计/钱包到账/本期”；S07 净额非负，删除负净额标签；S08 改查 `cs_product_public` 公开商品字段，不另开游客 profile。
- S09/S10 用 4 状态枚举；S11 按物流状态不把 `in_transit` 当签收；S12 按下单件数聚合、不混支付件数；S13/S14 的 `order_id/sku_id` 值来自 01 上下文，直连执行器可绑，链缺口另记；S15–S20 用左连接/钱包到账，防 fan-out、不把钱包退款当订单累计。
- S21/S22 澄清槽位；S23 退款率先 clarify（订单批次/支付 cohort 的分子分母不同），不一律 unsupported；S24 写操作只 unsupported，不保留任选响应。
- 01 原意见为 **19 query / 3 clarify / 2 unsupported**；03 把 S06 拆成 **S06C（clarify）** 与 **S06Q（已确认 scope 的 query）**，合计 **19 query / 4 clarify / 2 unsupported**，差异单独记录，不写成“01 意见”；S06Q 通过不算 S06C 澄清通过；合法多行 fixture 造 fan-out；不用负金额、向非空列填 NULL、重复主键等不合法正常样本。

### 12.3 02 修正

| 项 | 修正后 |
|---|---|
| 资源 | 当前真实存储是 JSON 快照；SQL 面 0；只保留四投影 `ops_order/ops_order_item/ops_refund/ops_sku`，全部“待实现/待批准”；撤回七资源可直接冻结 |
| 指标 | 8 标量 + `channels[]`；`OPS-METRIC-v0.1-review.1` 是当前实现记录，不是部署响应；字段、精度、未知原因、`as_of` 引用必须显式 |
| 截止/版本 | dashboard HTTP 无 `as_of/revision`；`snapshot_id` 指定取最高修订，不是“最新 watermark”；训练标签绑快照三元组+窗口+筛选+指标版本 |
| 成本/权限 | `cost.read` 由 02 投影控制；无权限删成本字段；有权限且成本齐全也不返回实际利润；03 不接收真实成本 |
| 零额退款 | 成功零额事件当前计入 cohort 退款订单率；待业务确认；不自行改标签 |
| 协议 | 02 建议用 03 `profile` 骨架，不用 `consumer_profile`；SQL 面未实现前只做指标意图+固定工具过渡，不混 SQL 样本 |

### 12.4 03 独立 PG 最小闭环（已执行）

- 环境：PostgreSQL 16.2 嵌入式；`sandbox/`；`csq_agent_query` 只读角色 + 会话变量 + RLS + 5 参数检查 + 2s/50 行/函数/字面量限制。
- 数据：FX-A/B/C 三组合成 fixture；8 题×3 组；每查询≥2 组有区分度结果。
- 结果：8 题×3 fixture 共 24/24 用例通过（含 S06Q；S06C 未执行）；17/18 安全用例按预期拒绝，1 例 RLS 异常（inactive store 可见）；所有失败/异常留日志，不改写成金标。
- 边界：只证明 03 合成 fixture 上的候选/校验/执行/断言；不证明 01 生产库、02 SQL 面、模型能力或端到端接入。

### 12.5 仍未决

01 契约版本/80+40 归属/RLS 修复/参数链改造排期；02 四投影采用/执行面/`as_of` 重放/零额退款/成本投影；三方联合签署时点；Codex 数据检验接口。以上未决前，不冻结联合契约、不扩训、不接 01/02。

## 附录 A：数据建设方案（v0.1）

### A.1 来源评估

| 来源 | 用途 | 许可/约束 | 责任/状态 |
|---|---|---|---|
| 01/02 源码、PRD、Schema/指标 | 定义问题族、字段、口径、约束 | 内部只读；以最新源码/PRD 为准 | 03 已核对 |
| 01 synthetic JSON、02 demo 快照 | fixture 种子/边界数据 | 内部合成；量小 | 03 已核对 |
| 公开 Text-to-SQL 数据 | 通用 SQL 能力（SFT/GRPO 辅助） | 逐数据集核 license；schema/方言不同；不做业务金标 | Codex 调研筛选；03 接收数据包 |
| 业务规则合成/适配 | 业务金标候选（问题+SQL+fixture） | 03 自建；必须匹配 01/02 获准契约；需审核 | 03 生成；Codex 独立检验 |
| 缺口生成 | 公开数据/业务适配覆盖不足的候选 | 03 生成；不得冒充金标；需执行+断言+审核 | 待启动 |
| 未来真实 CSV + 字段字典 | 分布校准、真实问句/值 | 需授权、脱敏、字段字典 | 未提供 |
| EXTERNAL-GEN API | 同义改写/候选生成/冻结对照 | 另批预算；不蒸馏；输出必须执行+断言+抽检 | 未批准 |

### A.2 样本结构（JSONL，字段可调整）

```json
{
  "sample_id": "cs-v1-0001",
  "profile": "customer_self",
  "question": "我上个月买了几单？",
  "normalized_question": "用户本人上个月已支付订单数",
  "intent": "aggregate_count",
  "schema_revision": "CS-QUERY-B2-v1",
  "schema_digest": "<sha256>",
  "metric_revision": "CS-METRIC-B2-v1",
  "metric_scope": "orders.created_at_window",
  "time_semantics": "created_at half-open [start_at,end_at)",
  "required_parameters": ["start_at", "end_at"],
  "provided_parameters": {"start_at": "...", "end_at": "..."},
  "expected_kind": "query",
  "gold_sql": "SELECT ...",
  "equivalent_sql": ["SELECT ..."],
  "clarify_slots": [],
  "expected_result": {"columns": ["order_count"], "rows": [[3]], "assertions": ["exact_rows"]},
  "fixtures": [
    {"fixture_id": "fx-001", "seed": "fx-001.sql", "assertions": ["row_count=3", "amount_minor=12345"]},
    {"fixture_id": "fx-002", "seed": "fx-002.sql", "assertions": ["empty_result_expected=false"]}
  ],
  "result_checks": {
    "bag_semantics": true,
    "null_equals_null": true,
    "order_matters": false,
    "amount_tolerance_minor": 0
  },
  "source_type": "business_adaptation",
  "source_dataset": "01-cs-contract",
  "source_version": "CS-QUERY-B2-v1",
  "source_license": "internal",
  "source": {"type": "synthetic_rule", "author": "...", "refs": ["PRD/4.2", "01/schema.py"]},
  "license": "internal",
  "review": {"status": "draft", "reviewer": null, "date": null},
  "codex_review": {"status": "pending", "reviewer": null, "date": null},
  "split": {"group": "G-count-time", "fold": "train"},
  "template_id": "T-count-window",
  "paraphrase_group": "PG-count-window-01",
  "paraphrase_of": null,
  "notes": ""
}
```

### A.3 切分与防泄漏

- 分组键：`template_id` + SQL 形状/指标 + 来源 + `paraphrase_group` + fixture 族；同组只进一个 fold。
- 先按模板/问题族/相近 SQL/来源切分，再扩写问法；同义改写不得跨 train/dev/test。
- 公开数据与业务数据分开切分、分开报告；公开数据不得进入业务冻结测试，也不能用公开数据分数代替业务能力。
- 去重：规范化问题、SQL AST/规范化文本、结果指纹；近重复人工复核。
- `test` 冻结；`dev` 只用于早停/奖励调参；GRPO prompt 只从 `train` 取；Codex 检验使用的留出集不参与训练/调参。
- SFT 与 GRPO 可复用训练任务，但不得用 dev/test 答案训练或调奖励。

### A.4 Fixture 与结果比较

- 每个 query 样本 ≥2 组 fixture；至少一组非空且能区分错 SQL；另配边界/空/NULL/重复/时区样本。
- 禁止“错 SQL 和正确 SQL 都返回空”也算通过；空结果必须由断言明确预期。
- 比较：列名/类型一致；多重集（bag）；NULL=NULL；金额精确到分；时区 Asia/Shanghai；仅问题要求排序时比较顺序；重复行按多重集计数。
- 关联 fan-out、半开窗边界、退款/取消、未知成本、负净额、权限边界单独建用例；跨 profile 的越权候选必须返回不支持/错误。
- fixture 必须可由种子/快照重建；期望结果由金标 SQL 在隔离沙箱执行生成并审核，不手填不可复现的行。
- 记录 fixture 版本、执行器版本、SQL 与结果指纹；换 fixture 或执行器后重跑受影响样本。

### A.5 审核流程

1. 起草（问题/SQL/fixture/断言/来源/版本/许可）。
2. 自动检查：schema/枚举/只读/资源/执行/结果断言/切分泄漏。
3. 独立审核：业务样本由 01/02 对应业务 owner 或用户指定审核人审核；公开数据由 Codex 筛选并核许可；03 不自审通过。
4. Codex 独立检验：对 03 提交的数据包复跑 fixture、核对来源/版本/断言/切分；检验结论不替代 01/02 业务签收。
5. 冻结：记录审核人/日期/版本/哈希；拒绝样本保留原因，不静默删除。
6. 复核：契约/指标/执行器修订时重跑受影响样本；公开数据与业务数据分别出报告。

### A.6 首版规模（Q1 建议）

| 阶段 | 01 customer_self | 02 operations_store | fixture | 目的 |
|---|---|---|---|---|
| A 小闭环 | 12–18 | 8–12 | 2/题 | 验证全链路与判定器；总计 20–30 题 |
| B 种子 v0.1 | 120–180 | 40–60（pilot） | 2–3/题 | 可训练/可评测的审核金标；规模为建议 |
| C 扩写 | 待覆盖/质量/算力评估 | 待 02 契约 | 复用/新增 | 不先堆量；扩写前先过 B 与基线 |
| 公开数据 | 只补通用 SQL | 只补通用 SQL | 按数据集 | Codex 筛选；不计入业务金标/业务测试 |

数字是建议；A 的 20–30 题是工程小闭环，B/C 的规模由覆盖缺口、审核产能、质量与算力共同决定，不在需求阶段锁死。

### A.7 数据交付包（Codex 独立检验）

| 类别 | 必填内容 | 说明 |
|---|---|---|
| 来源/版本 | 数据集名、URL/commit、license、抓取/转换时间、原始字段说明 | 公开数据与业务数据分列；缺 license 不进入训练 |
| 问题/profile | 原问题、规范化问题、profile、Schema/指标修订、参数约束 | 绑定 `CS-QUERY-B2-v1`/`CS-METRIC-B2-v1` 或 02 草案修订 |
| 期望响应 | `query`/`clarify`/`unsupported`/`error`；标准 SQL 或澄清槽位；禁止项 | 一题可多等价 SQL；clarify 必须列必要槽位 |
| 测试数据 | fixture 种子/快照、初始化命令、期望结果、结果断言 | 可重建；覆盖重复计数/空/NULL/金额/时区/边界/退款/权限 |
| 审核/切分 | 来源类型、审核状态、模板/来源分组、train/dev/test、同义改写关系 | 同义改写不跨集；公开数据不进入业务冻结测试 |
| 复现 | 执行命令、环境版本、随机种子、结果指纹 | 缺项写“未采集”，不估造 |

### A.8 01/02 口径隔离（数据标签必须显式绑定）

| 项 | 01 customer_self | 02 operations_store（草案） |
|---|---|---|
| 订单/支付时间 | 订单按 `created_at` 圈定；支付金额取这些订单的 `paid_minor` | 支付按 `paid_at` 事件时间统计 |
| 退款时间 | 退款取“同一批已支付订单”的 `refunded_minor`，不按退款条目重复计；窗口仍由 `created_at` 决定 | 退款按 `occurred_at` 事件时间统计 |
| 净额 | `paid_minor - refunded_minor`（同一订单 cohort） | `payment_minor - refund_minor`（各自事件窗口） |
| 数据标签 | `metric_scope=orders.created_at_window` | `metric_scope=payments.paid_at` / `refunds.occurred_at` |

同一自然语言问题在两个 profile 下可能对应不同时间口径；样本必须写明 profile、指标修订和 `metric_scope`，不得用 01 标签训练/评测 02，反之亦然。

## 附录 B：评测与奖励（v0.1）

### B.1 指标

- 执行有效 rate；语义结果等价 rate；kind accuracy；clarify slot precision/recall；安全违规率；p50/p95 延迟；tokens/s；显存；费用。
- 分 profile、问题族、模板、时间窗/退款/关联等维度报告；不只报总分。
- Base/SFT/SFT+GRPO 用同一冻结测试、同一执行器、同一奖励/判定修订；EXTERNAL-GEN 与 deepseek 结果另列，不冒充 Base。

### B.2 奖励分层（GRPO 草案）

1. **硬门禁**：可解析、Schema/修订/参数合法、只读、资源/行数/函数限制；不通过 → 0 或负分，格式/长度不得补偿。
2. **响应类型**：query/clarify/unsupported 与标签一致；clarify 必须含必要槽位；unsupported 必须有正当理由。
3. **语义**：多 fixture 结果等价比例；指标/列/聚合断言；至少一组非空区分 fixture。
4. **反作弊**：恒空、`WHERE 1=0`、硬编码结果、删除必要过滤、错误关联、忽略未知值、全拒绝/全澄清、格式/长度掩盖错误；命中 → 负分或作废。
5. **辅助项**：格式/长度仅小权重，且不得超过语义权重；危险/越权候选不得因辅助项得分。
6. **组内诊断**：记录各分量、正确率、组内方差、全零/无差异组、截断率；无区分度先诊断，不延长训练。
7. **环境错误**：执行器/沙箱故障与模型错误分开；环境错误不计模型负分，重试或作废并记录。

权重、组内候选数、KL、长度惩罚、温度等留待设计评审；以上只是分层原则，不是已批准参数。

### B.3 实验矩阵

| 阶段 | 模型 | 数据 | 评测 | 记录 |
|---|---|---|---|---|
| Base | Qwen3-8B-Base | 无训练 | 业务冻结测试 | 基线、失败类型 |
| 公开数据 SFT（可选） | Base + public SFT | 公开 train（Codex 筛选） | 公开 test；另列业务测试 | 只证明通用 SQL 能力，不作业务验收 |
| 业务 SFT | Base 或公开数据 warm start + 业务 train | 01 为主，02 候选/试点 | 业务冻结测试 | 增益/退化、解析率、长度 |
| 业务 GRPO | 业务 SFT checkpoint | 业务 train prompts | 同一业务冻结测试 | 奖励分量、组内差异、吞吐/显存 |
| 参考适配冒烟 | 适配后的 SFT 小试 | 20–30 题小闭环 | 同一执行器 | 只验证流程/格式，不代替业务评测 |
| 对照 | EXTERNAL-GEN 与临时 deepseek | 另列 | 另列 | 不冒充 Base |

- 公开数据与业务数据分表报告；公开数据 SFT 的增益不能直接写成业务能力提升。
- 若使用公开数据 warm start，必须与“不用公开数据”的同口径业务 SFT 对照，记录增益/退化与负迁移。
- Base/SFT/GRPO 的测试集、执行器、判定器、奖励修订必须一致；任何变更都要新开实验编号。

## 附录 C：资源估算（v0.1，未实测；最终见 `docs/02-design/显卡选型建议.md`）

> 本节只保留量级估算。首选/备选卡型、当前单价、预计时长、总费用、估算依据与待实测项，以设计文档和 AutoDL 实时价为准；实际租卡待方案与费用确认。

| 方案 | 显存 | 可行性 | 备注 |
|---|---|---|---|
| 本地 RTX 3060 Laptop 6GB | 6GB | 仅冒烟/CPU 校验 | 不训练 8B；无 torch |
| QLoRA 8B（4-bit） | 24GB 起 | 可能；需实测 | 权重约 5GB + 激活/优化器；seq/batch 受限 |
| LoRA 8B（bf16） | 48–80GB | 较稳 | 权重 16GB + 激活；80GB 更宽裕 |
| 全参 SFT 8B | 80GB+ | 单卡不首选 | 权重/梯度/Adam/主权重约 128GB 量级，需 ZeRO-3/offload |
| GRPO 8B | 48–80GB | 需实测 | 策略+参考+vLLM/rollout；24GB 不推荐 |

- 磁盘：底座约 16GB；数据/日志/checkpoint/合并/量化另计；建议数据盘 ≥100GB，按实际 checkpoint 数调整。
- 时长/费用：取决于数据长度、seq、batch、rollout 数与卡型；租前按 AutoDL 实时价核，记录单价/时长/实付。
- 以上是量级估算，不是承诺；先做 SFT 小试与 GRPO 生成+训练合计资源实测。

## 附录 D：当前不做

- 不租 GPU、不登录 AutoDL 下单、不下载 8B、不安装训练依赖、不启动训练/部署、不新增付费调用。
- 不把公开数据当业务金标；不把 03 生成的缺口候选直接当金标；Codex 独立检验不替代 01/02 业务签收。
- 不修改 01/02 代码、数据库、运行环境、演示数据、可信时钟、备份或签收索引。
- 子代理不得修改最终文档；只能写各自 `_research/*.md`，由主执行复核集成。
- 不把本 PRD 当作设计或训练批准；需求、设计、实施分别评审。
