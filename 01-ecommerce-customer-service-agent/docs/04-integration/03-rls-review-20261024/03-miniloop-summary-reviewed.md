# 03 独立 PG 最小闭环运行摘要（8 题×3 fixture）

- 运行文件：`sandbox/results/run-20261024T062418Z.json`；复制为 `miniloop-run-latest.json`
- run_id：`20261024T062418Z`；生成时间：2026-10-24T06:24:19.973108+00:00
- PG：PostgreSQL 16.2（pgserver 0.1.4）；psycopg 3.2.9；sqlglot 27.20.0
- 用例：24/24 通过；安全：17/18 通过；退出码：1（因 RLS-01 异常）
- 安全失败：RLS-01；异常：1

> 本批是手写候选 + 03 目标级沙箱；不是 01 生产库/02 SQL 面/模型评测；24/24 不能替代 01/02 验收。S06C 澄清分支未执行，S06Q 通过不算 S06C 通过。

## 用例结果

| 用例 | fixture | AST | 执行 | 结果断言 | 等价 SQL | 行数 | 结论 |
|---|---|---|---|---|---|---:|---|
| S01 | fx_a | ok | yes | True | True | 1 | count of orders with created_at in [Nov-01 00:00 +08, Dec-01 00:00 +08 |
| S02 | fx_a | ok | yes | True | True | 1 | registered orders.count cohort: created_at window + 4 statuses + paid_ |
| S03 | fx_a | ok | yes | True | True | 1 | single Shanghai day, no UTC bucket; half-open [Nov-30 00:00 +08, Dec-0 |
| S05 | fx_a | ok | yes | True | True | 1 | registered orders.paid_minor over created_at paid cohort; includes fre |
| S06Q | fx_a | ok | yes | True | True | 1 | S06C 的 clarify 分支见 25 分支计划；本用例只验证 user 已选“订单累计退款”后的 resolved query。等于  |
| S08 | fx_a | ok | yes | True | True | 1 | public regular price, not promotion; available=stock-reserved; product |
| S13 | fx_a | ok | yes | True | True | 1 | escaped order_id is a trusted direct-executor parameter; order_item pa |
| S24 | fx_a | ok | no | None | None | - | write request must not produce SQL; hand off to authorized business fl |
| S01 | fx_b | ok | yes | True | True | 1 | count of orders with created_at in [Nov-01 00:00 +08, Dec-01 00:00 +08 |
| S02 | fx_b | ok | yes | True | True | 1 | registered orders.count cohort: created_at window + 4 statuses + paid_ |
| S03 | fx_b | ok | yes | True | True | 1 | single Shanghai day, no UTC bucket; half-open [Nov-30 00:00 +08, Dec-0 |
| S05 | fx_b | ok | yes | True | True | 1 | registered orders.paid_minor over created_at paid cohort; includes fre |
| S06Q | fx_b | ok | yes | True | True | 1 | S06C 的 clarify 分支见 25 分支计划；本用例只验证 user 已选“订单累计退款”后的 resolved query。等于  |
| S08 | fx_b | ok | yes | True | True | 1 | public regular price, not promotion; available=stock-reserved; product |
| S13 | fx_b | ok | yes | True | True | 1 | escaped order_id is a trusted direct-executor parameter; order_item pa |
| S24 | fx_b | ok | no | None | None | - | write request must not produce SQL; hand off to authorized business fl |
| S01 | fx_c | ok | yes | True | True | 1 | count of orders with created_at in [Nov-01 00:00 +08, Dec-01 00:00 +08 |
| S02 | fx_c | ok | yes | True | True | 1 | registered orders.count cohort: created_at window + 4 statuses + paid_ |
| S03 | fx_c | ok | yes | True | True | 1 | single Shanghai day, no UTC bucket; half-open [Nov-30 00:00 +08, Dec-0 |
| S05 | fx_c | ok | yes | True | True | 1 | registered orders.paid_minor over created_at paid cohort; includes fre |
| S06Q | fx_c | ok | yes | True | True | 1 | S06C 的 clarify 分支见 25 分支计划；本用例只验证 user 已选“订单累计退款”后的 resolved query。等于  |
| S08 | fx_c | ok | yes | True | True | 1 | public regular price, not promotion; available=stock-reserved; product |
| S13 | fx_c | ok | yes | True | True | 2 | escaped order_id is a trusted direct-executor parameter; order_item pa |
| S24 | fx_c | ok | no | None | None | - | write request must not produce SQL; hand off to authorized business fl |

## 安全结果

- **ROLE-01**: PASS — 
- **RLS-01**: FAIL — inactive store scope must not expose its order
- **RLS-02**: PASS — active store scope sees only its own orders
- **RLS-03**: PASS — cross-customer scope gets no rows
- **RLS-04**: PASS — missing session scope fails closed or returns 0
- **SEC-01**: PASS — write statement rejected by validator
- **SEC-02**: PASS — star rejected except count(*)
- **SEC-03**: PASS — LIMIT above 50 rejected
- **SEC-04**: PASS — aggregate over join rejected for ambiguous grain
- **SEC-05**: PASS — parameter not declared is rejected
- **SEC-06**: PASS — window longer than 366 days rejected before execution
- **SEC-07**: PASS — JOIN ON TRUE rejected (no constant join condition)
- **JOIN-01**: PASS — JOIN self comparison must be rejected
- **AGG-01**: PASS — dead CTE must not bypass multi-view aggregate grain guard
- **FUNC-01**: PASS — non-whitelisted functions must be denied by default
- **LIM-01**: PASS — subquery LIMIT above 50 must be rejected
- **RLS-05**: PASS — scope-changing set_config must be rejected
- **RLS-06**: PASS — base table access must be rejected statically

## 异常

- **RLS-01**（rls_scope）：inactive store scope must not expose its order

## 哈希

- 00_roles.sql: `3e3d52fbed60d94985041a11f917413099c0492943d8cec01c3f6b1d6d91d019`
- 01_schema.sql: `370684a79b52382f7cfe7685ac8c095bdd2a36b6d0ad2fb75d56c222ad888643`
- fx_a.sql: `f426ea954148a3475b87809037d054d07ef53f9032845768c5e59c1e028cadad`
- fx_b.sql: `e13137170fb70f34bbfad5d7dc7ce79d00c015288753c65e8dc7938d033bddae`
- fx_c.sql: `82c6b65152d4ed61e9eab94e7fdf455063ec7d69dfde3a8e12481cdae7d9f34e`
- miniloop.json: `cb9b659515371570108d5d4358de5e40692cfe77ef5854775dbf2a6307627bb5`
- validator.py: `eb0d5dabe13f35652e2aed72799a7ca317163d1e572e164bff5ed490721280af`
- run.py: `3a22c53ddeed097c93ddf1ce945d3d4144375b58feabbb06fb1705c5ee7341a3`
- requirements.txt: `209b36ebc7734b4c6a1a732e170cb1c2de070d6ac51f735e7c07eb0b7e98cd2f`

## 未覆盖

- 25 分支中其余 16 题 + S06C 尚未补 fixture/断言。
- RLS-01 为异常，不是金标；待 01 复核后按明确版本同步沙箱再复跑。
- 01 validator 对照见 `docs/02-design/校验器对照.md`；3 例差异未决，不据此冻结数据。
