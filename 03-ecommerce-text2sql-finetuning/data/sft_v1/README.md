# 01 customer_self SFT 候选数据 v1（2026-10-24）

> **当前状态入口（ITER-20261025-01）**：见 [review-status.json](review-status.json)。历史独立签收与当前 train/dev/test 三个 hash 一致，签收范围是首轮 customer_self 离线数据子集；原始 JSONL/manifest 的 `candidate_unreviewed` 保留。不是全业务、模型效果、02 数据或训练费用批准。下方早期“未独立检验”等描述保留作历史，现状以侧车和原始签收证据为准。

状态：**candidate_unreviewed**；本包是首轮训练候选，不是业务金标。训练目标
由用户执行单 v4 指定：train 152 / dev 12 / test 20，共 184；22 个规范化
SQL 模板（组），组按“业务问题 + 规范化 SQL”先定，再扩写问法。

## 1. 文件与哈希

| 文件 | 行数 | SHA-256 |
|---|---:|---|
| `train.jsonl` | 152 | `a03798fa1aeae5639aa6f550413f1be62e21e667b337cfcda1e4420b95e7597b` |
| `dev.jsonl` | 12 | `dd34b5a6849062dd693fb8881626a35655e9aa9ce83f018cc0a8332084b13282` |
| `test.jsonl` | 20 | `469bd91ccaa3c92f5fa6745e267cf92ab669678089a4432e4a68a834827079bb` |
| `manifest.json` | — | 生成时记录上述哈希 |

`test.jsonl` 与冻结的 `data/eval_archive/test-v2.jsonl` 字节一致；`test-v1`
（`cf431555d87520d35faee61b965ca5c3450b341e00145a756641f443bb6b6c3d`）
保留在 `data/eval_archive/`。v1→v2 只改 E01-01..04 的题面措辞，SQL、参数、
窗口和 expected 未改。

## 2. 分布

| 项 | train | dev | test |
|---|---:|---:|---:|
| query | 120 | 12 | 12 |
| clarify | 24 | 0 | 4 |
| unsupported | 8 | 0 | 4 |

19 个 train 任务组：7 个 T query 组 + 8 个 N query 组 + T09/T10/T11 澄清 + T12 不支持；每组 8 条。全包 22 种规范化 SQL（15 train query SQL + dev/test 的 7 种），不是 22 个 train 组。T01 与旧 D01 合并；T05/N03 内含 sku_1→sku_2 参数变体；D02–D05 只在 dev。

## 3. 来源与边界

- 业务部分：03 手写/合成，基于 01 `CS-QUERY-B2-v1`/`CS-METRIC-B2-v1`、
  52 列、四注册指标、退款三 scope、半开窗和 0012 七项 GUC；**未使用真实
  顾客数据，未调用教师 API，未使用 01 的 80+40 验收答案**。
- 新增 8 个 SQL 种子：用户提供的 `首轮SFT新增8个模板候选.json`；已逐条
  通过 01 `validate_query` 与 03 validator。
- 公共数据（SQL-R1 SynSQL）下载/管线在 `data/raw`、`data/public_sft`、
  `data/sft_mixed` 保留，但**本轮未接入**；它们不进入 152 条。
- dev 保留 12 条，ID 前缀 `DV-`；D02–D05 只在 dev。D04 已恢复承运商签收的订单数，以外层 COUNT 套 DISTINCT order_id 计算；旧列表语义已撤回，原始记录见 `legacy-dev-results.md`。
- test 冻结；任何进一步改写都不得改 test。

## 4. 自查命令与结果

```bash
# 全量结构/分组/01+03 校验/144 条 query×3 fixture=432 次执行/20 条 test 业务 oracle
sandbox/.venv/bin/python training/check_dataset.py \
  --data data/sft_v1 --consumer01-root ../01-ecommerce-customer-service-agent \
  --output outputs/sft-v1-check.json

# tokenizer：184 条，1152 序列
sandbox/.venv/bin/python training/check_tokenizer.py \
  --model /tmp/qwen3-8b-base-tokenizer --data-dir data/sft_v1 \
  --max-seq-len 1152 --output outputs/sft-v1-tokenizer.json

# 五组评测器回归（source 01 + packaged01）
sandbox/.venv/bin/python training/test_preflight.py \
  --test data/sft_v1/test.jsonl \
  --consumer01-root ../01-ecommerce-customer-service-agent \
  --output-dir outputs/preflight-v1/source01
```

2026-10-24 本轮结果：

- `check_dataset`：152/12/20，19 个 train 任务组/15 种 query SQL，跨集 SQL/业务族 0，ID 唯一；01/03
  validator 全通过；144 条 query×3 fixture=432 次执行；test 12/12 query 业务 oracle
  通过，5 个 query 模板独立 expected 全部匹配；`ok=true`。
- tokenizer（Qwen3-8B-Base tokenizer，非权重）：184 条总 token
  **902–1051**，均值 951.55；`max_seq_len=1152` 时 **0 超长、0 label
  失败**；EOS id 151643；tokenizer 有 chat template，本训练使用 `common.prompt`，不套用它。
- 五组 preflight：source 01 与 packaged01 均通过；strict JSON 1.0；
  Markdown 包裹仅 lenient 1.0、strict 0；错 kind 固定分母 11/12；
  invalid/fake-cache 均 0。

## 5. 未解决/限制

1. 152 是**执行目标**，不是 152 个独立 SQL 问题；19 组内是改写，不是
   152 条独立监督。训练重复会改变有效 epoch，不能把行数当独特覆盖。
2. 历史首轮离线子集已独立签收，具体 hash、审核者归属和范围见侧车；未将其扩大为完整业务或联合契约批准。
3. dev 12 条均为 query，澄清/不支持只在 test；测试统计量有限。
4. 公共数据未混合进本轮；如后续接入，必须另出 manifest，不覆盖本包。
5. 训练需 GPU；本机仅 tokenizer/静态/沙箱验证，无 Base/SFT 结果。
