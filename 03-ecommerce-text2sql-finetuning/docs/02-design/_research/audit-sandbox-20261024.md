# 03 沙箱安全审计（2026-10-24）

审计对象：`sandbox/`（03 独立 PG 16.2 最小闭环）。只读审计，未改任何代码/DDL/fixture；本文件是唯一新增文件。
方法：静态读码 + `validator.validate()` 直接单元探针（50+43+37 例）+ 独立集群 `/tmp/sbx-audit/pgdata` 上以 `csq_agent_query` 受限角色执行（复用 `run.py` 的 `start_pg/setup_fixture/run_query`，fixture `fx_c`）。未连接 01/02 任何库。

## 1. 摘要

- 沙箱的“只读”主要靠**数据库授权**兜底：写/DDL 即使绕过 validator 也被 PG 拒绝（已验）。但**读隔离**不是：RLS 活跃店谓词退化，停用店数据可见（P0，已复现）。
- validator 的真正缺口不在 `ON TRUE`（**该写法实际被拒**），而在“JOIN 等值键语义不校验”“函数白名单 fail-open”“聚合粒度守卫可绕过”“LIMIT/ORDER BY 只查根节点”“参数替换用正则”。其中前三项可产生**静态通过、执行成功、业务错误**的查询。
- 执行器有 4 个可信度问题：出错也提交、行数上限是客户端事后检查、RLS 用例不走 validator、安全异常不影响退出码。
- 证据链有一处硬伤：`miniloop.json` 现哈希与运行日志记录不一致，`run.py` 未入哈希。
- 结论：**当前 24/24 不能作为“沙箱安全通过”的证据**；安全 10/11 的 1 个异常是真缺陷，且 11 项安全用例未覆盖上述高危面。

## 2. 缺陷表（P0/P1/P2，共 12 条）

| ID | 严重度 | 位置 | 复现（最小） | 影响 | 最小修复建议 |
|---|---|---|---|---|---|
| SEC-01 | **P0** | `sandbox/sql/01_schema.sql:207-314`（8 条策略；首条谓词 L215-216，其余 L229-230/243-244/257-258/271-272/285-286/299-300/311-312） | `fx_c` 下 `store_b` inactive、`cust_a` 有订单 C3；以 `cs.store=store_b, cs.owner=cust_a` 查 `cs_order_self` 返回 C3（期望 0）。`pg_policies.q_common.qual` 为 `EXISTS (... WHERE s.store_id = s.store_id AND s.status='active')` | RLS 活跃店谓词恒真，8 张业务表的停用店数据全部可见；沙箱“RLS 反例”成立，但沙箱自身**不能**作为“修复后验证” | 每条策略把内层 `store_id` 限定为所属外层表：`s.store_id = <表名>.store_id`（如 `customer_order.store_id`）；加断言 `qual` 不含 `s.store_id = s.store_id` |
| SEC-02 | **P0** | `sandbox/runner/validator.py:139-150` | `SELECT o.order_id,i.order_item_id FROM cs_order_self o JOIN cs_order_item_self i ON o.order_id=o.order_id` → validate ok，运行 **12 行**（正确 4 行）；`ON o.order_id=i.order_item_id` ok 且运行 0 行；`ON o.order_id=i.refunded_qty` ok、运行 `text = integer` 错 | 自比较/异键/类型不匹配的 JOIN 静态放行；能执行但结果按笛卡尔扇出，属“静默业务错误” | 要求 `on` 两侧均为 Column、**列名相同**、两侧 alias 不同且均属已声明视图、列名 ∈ {order_id,sku_id,shipment_id}；拒绝同一 alias 自比较。注：字面 `ON TRUE`/`ON 1=1`/`AND TRUE` 已被拒，本项是等价漏洞 |
| SEC-03 | **P0** | `sandbox/runner/validator.py:180-185` | `WITH d AS (SELECT 1) SELECT sum(o.paid_minor) FROM cs_order_self o JOIN cs_order_item_self i ON o.order_id=i.order_id` → ok，运行 **9000**（正确 5000）；`WHERE o.order_id IN (SELECT order_id FROM cs_order_self)` → ok 且 9000 | 跨表聚合重复计数被静默接受；死 CTE 或任意子查询即可关闭守卫 | 删掉 `not ctes and not any(Subquery)` 逃生口；改为按作用域判定聚合输入列所属表唯一，或强制多表聚合必须 `GROUP BY` 覆盖 join key；子查询逐层独立校验 |
| SEC-04 | **P0** | `sandbox/runner/validator.py:169-178` + `run.py:86-96,224` | 受限角色执行 `SELECT (SELECT set_config('cs.owner','cust_b',true)) AS s, (SELECT count(*) FROM cs_order_self) AS n` → n=**0**；反向顺序 n=**3** | RLS 作用域来自会话 GUC，**受限角色可在 SQL 内自行改写**；validator 拒 `set_config`，但 RLS 路径（run.py:224）不过 validator，故“RLS=数据库强制”不成立，实际是“validator 强制” | 短期：validator 函数默认拒绝（见 SEC-05）+ `run.py` 事务末断言 `cs.*` 与传入 scope 一致，不一致 fail；中期：评估 PG15+ `REVOKE SET ON PARAMETER`（需另做实验，本文不背书）；文档明确 scope 是评测器注入、非鉴权 |
| SEC-05 | **P1** | `sandbox/runner/validator.py:169-178` | `lower/upper/abs/length/random/now/current_user/extract/concat/greatest/md5/trim` 全部 validate ok，且多数运行成功（`current_user` 返回 `csq_agent_query`，`random` 每次不同） | 白名单只对 `Anonymous` 生效，其余具名函数默认放行（fail-open）；引入时钟/随机/角色名等非确定性或信息泄漏面，未来新函数自动获得执行权 | 改为默认拒绝：仅 `fname ∈ ALLOWED_FUNCTIONS` 且 AST 类为 Count/Sum/Min/Max/Avg/Coalesce/DateTrunc 时通过，其余一律 `unapproved_function`；删除 `nullif/round/cast` 特判（CAST 已被 AST 禁） |
| SEC-06 | **P1** | `sandbox/runner/run.py:86-96, 108-109` | 读码：`finally: ctx.__exit__(None,None,None)` 在异常路径也提交；`open_tx` 中 `SET LOCAL ROLE`/`set_config` 失败时事务上下文未退出 | 现在只读故写副作用为空；一旦执行器扩到写/临时对象/`SET`，失败语句的部分效果会被提交；单次角色失败会让连接停在 aborted tx，拖垮后续用例 | `except` 中 `ctx.__exit__(*sys.exc_info())` 回滚后重抛；成功路径才 commit；`open_tx` 改成 contextmanager 并在 `__exit__` 兜底 |
| SEC-07 | **P1** | `sandbox/runner/run.py:99-110` | `SELECT g FROM generate_series(1,2000000) g` 运行 520ms 后抛 `row_limit_exceeded`；`fetchmany(51)` 是客户端游标 | 上限是事后检查：服务端已算完、客户端已收全量；不保护内存/带宽/DB，错误分类为运行时而非静态拒绝 | 执行前包装 `SELECT * FROM (<sql>) q LIMIT 51`（注意保留 ORDER BY 语义），或服务端命名游标 + `itersize`；validator 对无根 LIMIT 的查询注入 `LIMIT 50` |
| SEC-08 | **P1** | `sandbox/runner/run.py:164-171, 224, 256-267, 305` | ① `unsupported` 直接记 `ast_ok/result_match/pass=True`（S24 3 例真空通过）；② `parameters` 用例内联重算 366 天规则、不调 `check_params`（SEC-06 自证）；③ RLS 用例不 validate；④ 退出码只看 `cases_fail`，安全异常不影响 | “24/24、安全 10/11”被高估；测试与被测逻辑重复实现，规则回归测不出；CI 可在 RLS 异常下绿灯 | ① 断言 unsupported 的 `sql` 为空且不执行，单独统计；② 直接调 `check_params` 并断言 code；③ RLS 用例先 validate（其 SQL 均合法）；④ `security_fail or anomalies` → 退出码非 0 |
| SEC-09 | **P1** | `sandbox/results/miniloop-run-latest.json`、`miniloop-summary.md`、`run.py:290-296` | 当前 `cases/miniloop.json` sha256=`cd08501213fa85a4850dddb1cb41eb1ba53ac54e82fe8296fe1c0480644e4d0f`，日志/摘要记 `7ad420d543ecc35bf2026d7e40d148e1f64d171d13a59a4a18c8988cf4b886af`；`run.py` 现 `7ef65f8357cd49610060460198dd145d3c649951815421f2a50af75a178995f0` 未入哈希 | 证据与当前输入不对应；S24 expected 由 null 变 "unsupported" 说明跑后编辑过；执行器改动不可追溯 | 重跑并按当前哈希出结论；哈希表加 `run.py`、`requirements.txt`；CI 校验哈希不符即 fail |
| SEC-10 | **P2** | `sandbox/runner/validator.py:199-213, 231-234` | 子查询 `... IN (SELECT order_id FROM cs_order_self LIMIT 1000)` ok；子查询 `OFFSET 999999` ok；`ORDER BY :order_id + 1` ok（`to_psycopg` → `%(order_id)s + 1`），而 `ORDER BY :order_id` 被拒 | LIMIT/OFFSET 上限只查根节点，可下推绕过；ORDER BY 参数可用表达式绕过，提供参数时退化为常量排序、未提供时执行期缺参 | 对 `tree.find_all(exp.Select)` 逐层校验 limit/offset；`exp.Ordered` 子树含 Placeholder 即拒 |
| SEC-11 | **P2** | `sandbox/runner/validator.py:250-253` | `WHERE paid_minor % 2 = 0` 静态 ok，运行 `ProgrammingError: incomplete placeholder: '%'`；`$tag$:start_at$tag$` ok，运行 `query parameter missing: start_at`；注释内 `-- :order_id` 同理 | 正则文本替换与 AST 校验语义不一致：合法 `%` 查询失败；占位符可出现在字面量/注释/美元引用中被误改，属潜在注入面 | 用 sqlglot 把 `exp.Placeholder` 重写为 `%(name)s`（或 psycopg 原生 `:name`），禁止对整串做正则 |
| SEC-12 | **P2** | `validator.py:118-128, 152-167, 187-197, 103-106`；`01_schema.sql:358-360`；`run.py:135-145` | ① `WITH a AS (SELECT ...) SELECT count(*) FROM a` → `unapproved_resource: table 'a' is not one of the six views`（≤2 CTE 承诺不成立）；② `SELECT balance_minor FROM cs_order_self` 静态 ok、运行 `UndefinedColumn`；③ `E'a\nb'`、`X'41'` ok；④ 8 张 base 表被整表 `GRANT SELECT`；⑤ `compare` 忽略列名与行数（`count(*) AS total` 也能过 S01） | 残余但会误导：合法查询被拒/越界查询靠 DB 报错兜底/列契约未断言/validator 一旦绕过即可读 base 表未暴露列 | 登记 CTE 名到作用域或明确移除 CTE；无别名列限单表；字面量按 `is_string` 规范化（含 E/X/dollar）；base 表改列级授权或 security_definer 视图；`compare` 断言列名集合、scalar/row 行数=1 |

## 3. 核对通过（本次实测/读码确认，不再列为缺陷）

- 角色：`csq_agent_query` 为 `NOSUPERUSER/NOBYPASSRLS/NOCREATEDB/NOCREATEROLE`（`pg_roles` 实测）。
- 写/DDL：未过 validator 的 `INSERT INTO app.customer_order ...` 运行即 `InsufficientPrivilege: permission denied for table customer_order`，事后计数仍 3；DB 授权对写有效。
- 超时：`generate_series(1,20000000)` 笛卡尔查询触发 `QueryCanceled: canceling statement due to statement timeout`（2000ms 生效）。
- 静态拒绝：`ON TRUE`/`ON 1=1`/`ON ... AND TRUE`/`USING`、`UPDATE`、`SELECT *`、`LIMIT 100`、367 天窗口、3 个 CTE、`UNION`、`CASE/CAST/LIKE/Window`、db 限定表、base 表直读、未声明参数，均按预期被拒（`unsafe_query/invalid_limit/invalid_window/invalid_parameters/sql_too_complex` 等）。
- 参数名白名单与“占位符集合=声明集合”生效；`ORDER BY :order_id` 直接占位符被拒。
- 资源上限存在：SQL≤8192B、AST≤512 节点、深度≤18、join≤3、根 LIMIT≤50、OFFSET≤10000。
- RLS-01 异常可稳定复现（`store_b` 返回 1 行）；`store_a` 返回 3、`cust_b` 返回 0，与用例期望一致。
- 视图 `security_invoker=true, security_barrier=true`；8 张业务表 `ENABLE+FORCE RLS`。

## 4. 建议新增用例（最小集）

| 用例 | 输入 | 期望 |
|---|---|---|
| JOIN-01 | `ON o.order_id = o.order_id` | `unsafe_query`（自比较） |
| JOIN-02 | `ON o.order_id = i.order_item_id` / `= i.refunded_qty` | `unsafe_query`（异键/类型不符） |
| AGG-01 | 死 CTE + 跨表 `sum(o.paid_minor)` | `ambiguous_grain` |
| AGG-02 | `IN (SELECT ...)` + 跨表聚合 | `ambiguous_grain` |
| AGG-03 | 正确 `GROUP BY o.order_id` 版本 | `ok` + 业务值（防过度拒绝） |
| FUNC-01 | `lower(...)`/`now()`/`random()`/`current_user` | `unapproved_function` |
| LIM-01 | 子查询 `LIMIT 1000`、`OFFSET 999999` | `invalid_limit` |
| ORD-01 | `ORDER BY :order_id + 1` | `unsafe_query` |
| LIT-01 | `$tag$:start_at$tag$`、`E'paid'`、`X'41'` | 参数/字面量拒绝或正确参数化 |
| EXEC-01 | `paid_minor % 2 = 0` | 修复后应 `ok` 且结果正确（回归 SEC-11） |
| RLS-05 | 同语句 `set_config('cs.owner',...)` + 读 | `unsafe_query`；且执行后断言 scope 未变 |
| RLS-06 | `SELECT order_id FROM app.customer_order` | `unsafe_query` + DB 层 `InsufficientPrivilege` |
| CMP-01 | `SELECT count(*) AS total ...`（S01 变体） | 断言失败（列名/别名契约） |
| EVID-01 | 跑后比对 `hashes` 与实际文件 | 不一致即 fail |

## 5. 已验 / 未验边界

**已验**：上述静态规则、运行时错误分类、写拒绝、超时、RLS-01 复现、GUC 改写作用域、行数上限的事后性、证据哈希漂移。运行时使用独立集群 `/tmp/sbx-audit/pgdata` 与 `fx_c`，未触碰 `sandbox/results/` 的既有运行记录；探针脚本在 `/tmp/sbx-audit/probe{1,2,3,4}.py`。

**未验**：① 01 生产库的 DDL/RLS/grant/驱动/成本阈值与本沙箱是否一致（沙箱按 01 答复重建，非同一实现）；② 01 的 validator；③ 02 的 SQL 面（仍只有 JSON 快照）；④ 模型输出（手写候选，未跑 Qwen3-8B-Base）；⑤ 当前 cases 哈希下的完整 8 题重跑（日志与当前文件不对应）；⑥ `open_tx` 泄漏的真实触发；⑦ 多表歧义列的运行时行为（仅静态判定）；⑧ `ORDER BY` 表达式提供参数后的实际排序；⑨ `REVOKE SET ON PARAMETER` 可行性；⑩ 并发/锁/性能。

**边界声明**：本审计只针对 03 沙箱。沙箱不是 01 生产库（DDL/RLS/驱动/成本均可能不同），不是 02 SQL 面，也不是模型评测；文中所有“通过/失败”仅指该沙箱的静态校验、执行、业务断言三层之一，三者不能互相替代。
