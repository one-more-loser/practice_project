# 03 文档一致性审计（2026-10-24 13:49 +08:00）

> 本文件是**文档一致性审计记录**，不是 Codex 验收，不构成需求批准、设计冻结、数据金标或训练批准。审计只读，未修改任何最终文档或 01/02 文件。

## 0. 摘要

| 项 | 值 |
|---|---|
| 审计对象 | `README.md`、`HANDOFF.MD`、`docs/01-requirements/*`、`docs/02-design/*`（含 `_research/`）、`docs/03-data/01-小闭环计划.md`、`docs/04-evidence/*`、`sandbox/README.md` 及 `sandbox/` 实际产物 |
| 比对基准 | 文件实读 + `sandbox/sql/01_schema.sql`、`sandbox/cases/miniloop.json`、`sandbox/fixtures/fx_{a,b,c}.sql`、`sandbox/results/*` 实计数（非引用文档自述） |
| 审计时刻 | 2026-10-24 13:49:29 +08:00（全部最终文档 mtime 13:47:02–13:47:10） |
| 发现问题 | **14 条**：P0 阻断 2 条、P1 7 条、P2 5 条 |
| 阻断项 | ①`PRD.md:146` 分视图列数合计为 **50**（与 52 冲突）；②`docs/04-evidence/2026-10-24-docs-manifest.md` 哈希全部失效且漏一份文档 |
| 总体判断 | 主体口径（52 列总数、18/4/2、11/12、8 题×3 fixture）已统一；剩余问题集中在**分视图列数残留、证据包失效、证据路径/轮次/D 编号等索引性错误**。 |

## 1. 问题表（按严重度）

严重度定义：**P0** = 会让下游数据/验收按错误事实构建；**P1** = 明示事实错误或索引指向不存在/过期对象；**P2** = 口径瑕疵、时点未标注、易误读。

| # | severity | 文件:行 | 现有文字 | 应为 | 证据 | 建议 |
|---|---|---|---|---|---|---|
| 1 | **P0** | `docs/01-requirements/PRD.md:146` | 「视图：`cs_order_self`(14)、`cs_order_item_self`(9)、`cs_shipment_self`(6)、`cs_wallet_summary_self`(4)、`cs_wallet_entry_self`(8)、`cs_product_public`(9)」 | `cs_order_item_self`(**10**)、`cs_shipment_self`(**7**)，其余不变，合计 52 | 14+9+6+4+8+9=**50**，与本文件 L338「52 列（订单行 10、发货单 7）」自相矛盾；`sandbox/sql/01_schema.sql:322-331` 实列数：order_item 10、shipment 7；`docs/02-design/01契约对齐评审包.md:29` 亦写 10/7；`_research/contract-mapping.md:56,57` 逐列列举同为 10/7 | 该行是「01 侧硬约束（源码事实）」，下游 schema/反例直接引用；必须改为 10/7，避免"50 列"以分解形式残留 |
| 2 | **P0** | `docs/04-evidence/2026-10-24-docs-manifest.md:1-21` | 12:38 生成的 17 个文件 SHA-256 + 字节数清单 | 重新生成；并补 `docs/02-design/三方契约差异表.md` | 全部文档 mtime 13:47:02–13:47:10，内容已改写；实测 README 3994B（清单 3756）、PRD 50503B（清单 43228）、质量 17702B、小闭环 10909B（清单 9841）；清单缺 `三方契约差异表.md`（该文件 120 行、13:35 版，正文被 README L5/L16、评审记录 L8、计划 L8 引用） | 证据包当前无法复算任何哈希；建议在所有文档定稿后统一重刷 manifest，并把"缺文件"作为必检项 |
| 3 | P1 | `README.md:25` | 「`sandbox/` \| 03 独立 PG 16.2 沙箱、fixture、runner、**summary.json**」 | 「…runner、`results/miniloop-summary.md`、`results/miniloop-run-latest.json`」 | `sandbox/` 下无 `summary.json`（亦无 `schema_hash.txt`）；实际产物为 `sandbox/results/{miniloop-summary.md, miniloop-run-latest.json, rls-anomaly-20261024.json, run-*.json}` | 文档表指向不存在的文件，评审方按图索骥会失败；改为真实路径 |
| 4 | P1 | `HANDOFF.MD:71` | 证据索引「评审记录 \| **第 1–4 轮**；未评审通过」 | 「第 **1–5** 轮；未评审通过」 | `需求评审记录.md:1` 标题「第 1–5 轮」、L9「第 1–5 轮已答（§10–§13）」、§13 为第 5 轮（13:10）；`README.md:5`、`README.md:16` 均写第 1–5 轮 | 与两份文档直接冲突，属索引未随第 5 轮更新 |
| 5 | P1 | `README.md:40` | 「需求规格评审（PRD v0.4 + 矩阵 v0.3 + **记录 §10–§12** + 四份方案草案）」 | 「记录 **§10–§13**」 | 评审记录第 5 轮为 §13（L276 起「## 13. 第 5 轮」）；L9 明写「§10–§13」 | 与第 5 轮存在事实不符；与 #4 同源，建议一次改净 |
| 6 | P1 | `HANDOFF.MD:105` | 「数据交付包…按 PRD §8 / **数据适配方案 v0.1** 执行」 | 「数据适配方案 **v0.2**」 | `docs/02-design/数据适配方案.md:5` 版本 v0.2（13:40）；评审记录 L8/L308、计划 L8、README L5 引用的均为 v0.2 | 交付包若按 v0.1 制作会漏掉 52 列/退款三 scope 等修订 |
| 7 | P1 | `docs/01-requirements/需求追踪与验收矩阵.md:38` | 「FR-SQL-025 \| 03 独立 PG **8 题/24 fixture** /RLS 反例 \| … \| 已执行（候选）」 | 「8 题×**3 fixture（24 用例）**」 | `sandbox/fixtures/` 仅 3 个 fixture 文件；`miniloop-summary.md` 记录 8 题×3 组=24 用例；本文件 L99 已正确写作「8 题×3 fixture；24/24」 | 同一文件内 L38 与 L99 两种写法；"24 fixture"会被读成 24 个 fixture 文件 |
| 8 | P1 | `docs/01-requirements/需求评审记录.md:303,304` | L303「成本/零额退款 \| … \| 接受；**D-23**」；L304「协议 \| … \| 接受；**D-24**，适配层处理差异」 | L303 → **D-23 应为协议映射**（PRD D-23=协议映射），成本/零额退款应为 **D-22 之后的独立项**；L304 协议应为 **D-23**，D-24 是「03 独立 PG 最小闭环」 | `PRD.md:308-311`：D-21=02 资源四投影、D-22=02 快照/截止、D-23=协议映射、D-24=03 独立 PG 小闭环、D-25=RLS；同表 L299 资源→D-21 ✓、L302 截止→D-22 ✓ 均正确，仅 L303/L304 错位 | 决策编号是追溯锚点，错位会使"哪条决定改了哪份文档"无法回溯；按 PRD §12.1 表逐条对齐 |
| 9 | P1 | `docs/02-design/02契约对齐评审包.md:282,388` | L282「11 月退款率 \| 无定义 \| 0.666667/0.500000 \| 02 需澄清 order/amount；01 **`unsupported`**」；L388「… clarify 或 query \| order/amount 二选一；01 **unsupported**」 | 01 侧不再一律 `unsupported`：「先 clarify 口径（订单批次/支付 cohort 分子分母不同），有可信依据才 query」 | `PRD.md:341`「退款率 \| 不一律标 unsupported…先 clarify」、`PRD.md:353` S23「退款率先 clarify…不一律 unsupported」、`需求评审记录.md:290` 01 答复「退款率先 clarify」、`需求追踪与验收矩阵.md:59` BR-SQL-016 同 | L388 同一行既写「clarify 或 query」又写「01 unsupported」，自相矛盾；该包仍是 02 评审在用材料，属旧判断残留 |
| 10 | P2 | `docs/01-requirements/需求追踪与验收矩阵.md:6` | 「日期 \| 2026-10-24 **13:55** +08:00」 | 与 README/HANDOFF 的 13:50 同源，或写实际写入时刻 | 审计时刻 13:49:29；全部文件 mtime 13:47:0x；矩阵内容并不比 README/HANDOFF 新。同类：数据/质量/小闭环标 13:40、README/HANDOFF 标 13:50，均晚于各自 mtime | 版本表头时间晚于文件自身写入时刻且晚于全局"当前点"，会让"最新文档是哪份"判断失真；建议统一为一次定稿时间 |
| 11 | P2 | `docs/02-design/数据适配方案.md:364` | 「工程小闭环 \| **20–30 题（01 为主，含 5–8 题 02 pilot）**」 | 「24 题（当前计划，全部 01）；20–30 题含 02 pilot 为目标态」 | `docs/03-data/01-小闭环计划.md:7`「只覆盖 01 `customer_self`；**02 不进入本闭环**」；L54 覆盖核对 24 题全为 01；计划 §6 亦写 02 边界 | 方案表把目标态写成当前态，且与唯一在执行的 24 题计划冲突；加"当前/目标"两列即可 |
| 12 | P2 | `docs/01-requirements/需求评审记录.md:211,266` | L211「01 小闭环计划… \| 已完成计划；**未执行**」；L266「01 小闭环沙箱/FX-A \| 03 \| … \| **未开始**；待契约答复后优先」 | 标注轮次时点，或补第 5 轮状态：8 题×3 fixture 已执行（24/24） | 第 3 轮（11:31）/第 4 轮（12:35）时点属实，但表内「未执行/未开始」无时点标注，与现状（计划 L6、PRD D-24、计划 L75）冲突 | 保留历史但加「（截止 11:31）」；否则读表即得错误现状 |
| 13 | P2 | `docs/01-requirements/PRD.md:156`；`PRD.md:229` | L156「逻辑资源：`ops_order`、…、`ops_payment`、`ops_cost`、`ops_inventory`、`ops_sales_period`、`ops_traffic_period`（草案）」；L229「净实付=支付-退款，**可为负**」（未限定 profile） | L156 标注「已按 D-21 收敛为四投影 `ops_order/ops_order_item/ops_refund/ops_sku`，其余仅 02 草案、03 不使用」；L229 限定「02 现金期可负；01 订单净额非负」 | `PRD.md:308` D-21、`PRD.md:360`、`需求评审记录.md:299`、`三方契约差异表.md:16`、`数据适配方案.md:48` 均为四投影、撤回七资源；`PRD.md:340/351`、`质量验收方案.md:15/109`、`计划 L16` 均为 01 非负 | 正文仍以 9 资源形态列出，与决策表并列易被当成"02 已有这些表"；BR-SQL-005 未限定 profile，01 侧会被读成可为负 |
| 14 | P2 | `docs/02-design/子代理任务与文件边界.md:48`；`docs/02-design/01契约对齐评审包.md:9`；`docs/02-design/参考项目复用与修改清单.md:7` | L48「`_research/contract-mapping.md`（53,932 B）；01/02 差异、**Q-01..Q-09**」；01 包 L9「03 **PRD v0.3**」；参考清单 L7 上游「PRD v0.3」 | L48 → 「Q-01-01..12 / Q-02-01..12（契约映射内仅 Q-01/Q-02 两条独立问题）」；01 包/参考清单的 v0.3 需标「（撰写时点 12:36/12:00，现 PRD v0.4）」 | `01契约对齐评审包.md:12` 输出物「Q-01-01..12」、`需求评审记录.md:233` 同；`_research/contract-mapping.md:440-441` 仅 Q-01、Q-02 两条问题；现 PRD 为 v0.4 | 两处 v0.3 在撰写时点属实，但未标时点会被当作现行依据；Q 编号数量与评审包不一致 |

## 2. 核对通过项（未发现问题）

| 检查项 | 结论 | 依据 |
|---|---|---|
| 6 视图 / **52 列（总数）** | 通过（除 #1 的分视图分解） | PRD L79/L305/L338、矩阵 L105、质量 L15、数据方案 L24/L43、计划 L8、01 包 L29/L102、sandbox/README L8 均为 52；`01_schema.sql` 实列 14+10+7+4+8+9=52 |
| 24 题覆盖 18 query / 4 clarify / 2 unsupported | 通过，无 19/3 残留 | PRD L354；评审记录 L308；数据方案 L47；质量 L18；计划 L54（clarify=S06/S21/S22/S23，unsupported=S04/S24）；grep「19/3」「3 unsupported」零命中 |
| FX-A/B/C 行数表与 fixture 实读 | 通过 | 实读：fx_a 订单 13/明细 14/物流 2/账户 2/流水 8/商品 3/SKU 4/库存 4；fx_b 7/7/1/1/1/2/2/2；fx_c 4/5/0/1/1/2/2/2；计划 L58–L62 三行逐一相符 |
| 8 题最小批次清单 | 通过 | 计划 L75 现为「S01/S02/S03/S05/S06-resolved/S08/S13/S24」，与 `miniloop.json` cases ids 及 summary 记录完全一致（注：13:47 前该行曾误写 S07 且漏 S24，现已修正） |
| 安全 11/12 与 SEC-07 | 通过 | `miniloop-summary.md:6` 11/12、security 12 行；计划 L77、PRD L311/L371、评审记录 L310、质量 L22、三方表 C-19、README L5、HANDOFF L4 全部 11/12；「10/11」零命中 |
| 沙箱证据路径（除 README:25） | 通过 | 计划 L76/L87、HANDOFF L80、PRD L367-L372 引用的 `sandbox/results/miniloop-summary.md`、`miniloop-run-latest.json`、`rls-anomaly-20261024.json`、`sandbox/sql/*`、`sandbox/fixtures/*`、`sandbox/runner/*` 均存在 |
| 「已执行 / 未执行」边界未夸大 | 通过 | 计划 L9/L93-L100/L110 明写「全量 24 题尚需补 16 题」「不因 8 题自动宣称完成」；PRD L372 明写不证明 01 生产库/02 SQL 面/模型能力/端到端；HANDOFF L15 未跑模型未训练；02 SQL 面全库均为「0/未实现」，无「02 已有 SQL 面」残留 |
| 版本号一致 | 通过 | PRD v0.4、矩阵 v0.3、数据 v0.2、质量 v0.2、24 题计划 v0.2、评审记录第 1–5 轮，与 README L5/L16、HANDOFF L14、评审记录 L308 一致 |
| 旧判断「50 列」字样 | 通过 | grep「50 列」「50列」「50 个列」「schema_hash」零命中（分解形态见 #1） |
| 旧判断「退款率一律 unsupported」 | 通过（最终文档） | PRD L341/L353、评审记录 L290、矩阵 L59、数据方案 L112/L134、质量 L16 均为「先 clarify」；仅 02 评审包见 #9 |
| 旧判断「七资源可直接冻结」 | 通过 | 5 处（PRD L308/L360、评审记录 L299、三方表 L16、数据方案 L48）全为「撤回/不冻结」否定式 |
| 旧判断「01 订单净额可为负」 | 通过 | PRD L306/L340（01 非负、02 可负）、评审记录 L289（删「01 净额可负」）、矩阵 L58、质量 L15/L109、数据方案 L25/L45/L341、计划 L16；无未限定 profile 的 01 负净额表述（BR-SQL-005 见 #14） |
| `sandbox/README.md:35` 安全用例数 | 通过 | 现为「8 题（7 query + 1 unsupported）与 12 个安全用例」，与 `miniloop.json`（RLS-01..04 + SEC-01..07）及 summary 的 11/12 一致（审计期间 13:47 已由 6 改 12） |
| 24 题 / 8 题运行证据自洽 | 通过 | `miniloop-run-latest.json` summary：cases_total=24、cases_pass=24、security_total=12、security_pass=11、anomalies=[RLS-01]，与 summary 文档一致 |

## 3. 审计边界声明

1. **本审计不是 Codex 验收**，不作为需求批准、设计冻结、数据金标、训练或租卡依据。
2. 审计只读：未修改任何最终文档、未改 01/02、未安装/下载/训练/租卡/付费/调模型；唯一新建文件为本记录。
3. 行号为审计时刻（2026-10-24 13:49，全部文档 mtime 13:47）快照；若后续再改写文档，需重新核对。
4. **审计期间文档在变动**：13:50–13:52 期间 `HANDOFF.MD`、`PRD.md`、`需求评审记录.md`、`三方契约差异表.md`、`数据适配方案.md`、`01/02契约对齐评审包.md`、`01-小闭环计划.md` 被改写。表中 14 条已在改写后（13:53）逐条复核仍然成立，行号为复核时行号；其中 `sandbox/README.md` 的安全用例数（6→12）与 `01-小闭环计划.md` 的最小批次清单（S07→S24）已在此期间修正，故移入第 2 节「核对通过」。
5. 未验证项：01/02 源码内部行号与行为（仅以 03 文档记载为据）；fixture 断言的业务正确性；24 题全部题目未在沙箱运行（仅 8 题）。
