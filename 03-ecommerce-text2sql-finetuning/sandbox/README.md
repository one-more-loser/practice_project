# 03 独立 PostgreSQL 最小闭环

状态：草案；**只验证 03 的目标级 SQL 候选/校验/隔离执行/结果断言**。不是 01 生产执行器、不是 02 SQL 面、不是模型输出；不连接 01/02 数据库，不使用凭据，不训练。

## 1. 边界

- 数据库：嵌入式 PostgreSQL **16.2**（`pgserver==0.1.4`），数据目录默认 `/tmp/03-sql-pgdata`。D: 是 drvfs，目录权限 777，`initdb` 拒绝；故运行数据放 Linux 临时盘，DDL/fixture/代码仍在 03。
- 关系：`sandbox/sql/01_schema.sql` 按 01 `0012_query_scope` 的六视图/RLS/角色语义重建；**不是 01 生产库**。基础表、约束、视图列数按 01 答复（52 列）重建。
- 角色：`csq_agent_query`，`NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS`；只读、只能通过 `SET LOCAL ROLE` 使用。
- 身份：`cs.owner/cs.store/cs.batch/cs.generation/cs.role` 由评测器在事务内设置；模型输入/样本不含身份、DSN、密码。模型生成的 WHERE 不是鉴权。
- 执行：单条 SELECT；`statement_timeout=2000ms`、`lock_timeout=500ms`、UTC 会话、`LIMIT<=50`、SQL<=8192B、AST<=512 节点、<=3 join、<=2 CTE、深度<=18、5 个参数名；03 本地 validator 不是 01 validator。
- 结果：AST 通过、SQL 可执行、业务结果正确**分别记录**，不互相替代。

## 2. 复现命令

```bash
# 需要 Python 3.12 + uv；uv 可临时安装到 /tmp，不写系统/DSH 配置
cd /mnt/d/codex_project/项目实战/03-ecommerce-text2sql-finetuning
/tmp/uv/uv venv sandbox/.venv --python 3.12
/tmp/uv/uv pip install --python sandbox/.venv/bin/python -r sandbox/requirements.txt
PGSERVER_DATA=/tmp/03-sql-pgdata sandbox/.venv/bin/python sandbox/runner/run.py
```

首次运行会由 `pgserver` 初始化并启动 PG 16.2。脚本每次按 `fx_a`/`fx_b`/`fx_c` 重建 schema、重放 fixture、跑 8 题×3 组 fixture，并执行安全/RLS 检查。

## 3. 文件

| 文件 | 用途 |
|---|---|
| `sql/00_roles.sql` | 受限只读角色 |
| `sql/01_schema.sql` | 6 视图/52 列、基础表、约束、RLS、grant |
| `fixtures/fx_a.sql` | 主数据集：6 种 trade_status、运费/优惠、多 SKU、部分/全额退款、钱包 |
| `fixtures/fx_b.sql` | 上海/UTC 月界、空/小结果、边界订单 |
| `fixtures/fx_c.sql` | 合法多行 fan-out、停用店 RLS 反例 |
| `cases/miniloop.json` | 8 题（7 query + 1 unsupported）与 18 个安全用例 |
| `runner/validator.py` | 03 本地 AST/参数/资源校验 |
| `runner/run.py` | 启动、重建、执行、断言、写 JSON 日志 |
| `results/` | 实际运行日志与 RLS 异常证据 |

## 4. 8 题与断言（候选，不是金标）

| ID | 能力 | 预期 |
|---|---|---|
| S01 | 上海 11 月创建总订单数（含未付/取消） | 不等于 `orders.count`；FX-A/B/C=9/5/3 |
| S02 | 同一建单 cohort 的已支付订单数 | 4 状态 + `paid_minor>0`；FX-A/B/C=7/3/2 |
| S03 | 上海 11-30 单日订单数 | 半开窗；不使用 UTC 桶 |
| S05 | 建单 cohort 原实付（含运费） | 不扣后续退款 |
| S06Q | 建单 cohort 累计退款（已解析分支） | 含窗后退款，不是钱包到账；S06C clarify 本身不在本批 |
| S08 | 公开商品常规价/可售量/版本 | 活动价不承诺；`available=stock-reserved` |
| S13 | 指定订单的订单行/退款数量 | 直接执行器 `order_id` 参数；不是客服端到端 |
| S24 | 写请求 | 只 `unsupported`，不生成/执行 SQL |

> 表中数字以 `results/run-*.json` 的 `expected/actual` 为准；文档冻结前不得手抄成金标。

## 5. 安全/RLS 结果（0012 校准后 18/18）

- 角色属性：reader 不是 superuser、不能 bypass RLS。
- 写、`SELECT *`、`LIMIT>50`、跨 view 聚合、未声明参数、367 天窗口均被 03 validator/参数检查拒绝。
- **RLS 反例（0012 校准后通过）**：`fx_c` 中 `store_b` 为 inactive，`cust_a` 在该店有订单 C3；以 `cs.store=store_b, cs.owner=cust_a` 查询 `cs_order_self` 返回 0（期望 0）。旧 0011 策略的 RED 批次保留在 `results/run-20261024T062418Z.json`；0012 校准记录见 `results/calibration-0012.md`。
- 该异常只作为 03 沙箱证据，**不写成金标、不修改 01、不据此放宽训练**；由用户反馈 01 修复/复核。

## 6. 未证明

- 0012 目标级 RLS 已校准，但未证明 01 真实 LOGIN/列权限/视图映射、生产库 DDL/驱动/成本阈值一致；未证明 01 客服已能绑定指定日期/订单/SKU/状态（当前只有 start/end，且固定 30 天）。
- 未证明 02 的 SQL 面；02 仍只有 JSON 快照与固定指标，`ops_*` 投影待实现。
- 未运行模型、未训练、未租 GPU；SQL 是手写候选，不是 Qwen3-8B-Base 输出。
- 8 题结果只对合成 fixture 有效；数据仍是候选，交 Codex 独立检验。
