# XiYan-SQLTraining 调研（数据处理 / SFT / 评测）

> 子代理 B 只读调研稿 v0.1 · 2026-10-24 11:42 +08:00 写入 · 证据截止 commit `6e102ff35b2c95c0c197d9a0454608efe6b0dc68`（2026-08-31）。
> 本文件仅为调研输入，不是需求批准、训练批准或数据金标；主执行另行复核后决定采纳与否。
> 标注约定：**【实现】**=仓库当前代码行为；**【README】**=README 声称；**【推断】**=笔者推断，未在仓库内验证。

## 0. 结论摘要

1. **能直接借的只有“数据装配 + M-Schema + 模板 + 执行比对”这套脚本骨架**，不是可运行流水线：目标仓库是 **SFT-only**，GRPO 至今未实现（`README.md:15` 的 `- [ ] Continued GRPO training for Text2SQL` 未勾选，`requirements.txt` 无 `trl`）。本项目“SFT→GRPO”中 **GRPO 段零可复用**。
2. **多方言支持是真的**（PostgreSQL/MySQL/SQLite/Cypher/ngql 模板与连接器都在），但评测与训练默认样例全是 SQLite/BIRD；PG 路径需要自测，且其 SQLite 库文件路径有硬约定（`{db_host}/<db>/<db>.sqlite`，见 `schema_engine.py:44-47`）。
3. **底座解耦**：训练侧用 `AutoModelForCausalLM` + `tokenizer.apply_chat_template`，不注入自定义特殊 token，换 Qwen3-8B-Base 只需处理“Base 无 chat_template”和版本约束，不依赖其 Qwen2.5-Coder 权重。
4. **最大不兼容不是模型而是三处**：① `transformers==4.42.3` 不支持 Qwen3（需 ≥4.51）；② 评测先 `max_new_tokens=10`、再 `[:40]` 截断（`sql_infer.py:114`、`:143`），开箱即坏；③ 预测 SQL 直接在当前连接执行、无只读事务与语句超时（`sql_utils.py:60-71`），放到 01/02 真实库上是危险面。
5. **对我们最有价值的三个设计**：M-Schema 文本表示 + 每列 `Examples` 采样；训练期 `SchemaShuffle/SchemaFilter` 做“弱 schema linking”增强；EX 用 `result_eq(order_matters=False)` 做列置换/行序无关比对。三者都能落到我们的契约上，但**过滤掉 PII 与禁用列、指标口径校验、validator 前置**必须由我们补。
6. **数据侧几乎没有工程**：无去重、无难度分层、无质量过滤、无 train/val 切分；只有随机采样与长度截断（`data_assembler.py:36-39`、`:50-60`）。把它当“格式参考”而不是“数据方案”。
7. 许可证 **Apache-2.0**，代码复用无障碍；随仓库分发的 BIRD 子集（221 条）另有上游许可，本项目不需要。

## 1. 仓库事实

| 项 | 值 | 证据 |
|---|---|---|
| URL | https://github.com/alibaba/XiYan-SQL | — |
| 调研子目录 | `XiYan-SQLTraining/`（仓库根同层还有 `BridgeSQL/`，为后续加入的另一套代码） | `ls`；`git log -1` 标题 `Add BridgeSQL code release` |
| commit | `6e102ff35b2c95c0c197d9a0454608efe6b0dc68`（2026-08-31 17:06:55 +0800） | `git rev-parse HEAD` |
| 克隆方式 | `git clone --depth 1` 到 `/tmp/research/xiyan-sql`，未改仓库 | 本次操作 |
| 许可证 | Apache License 2.0（仓库根 `LICENSE`） | `head -3 LICENSE` |
| 规模 | `XiYan-SQLTraining` 下 58 个文件、约 1.5 MB | `find . -type f \| wc -l`；`du -sh` |
| 首次发布 | 2025-10-30 发布第一版训练框架 | 【README】`README.md:5` |
| 目录 | `data/`（处理+增强+装配）、`train/`（SFT 入口、Trainer 子类、脚本、configs）、`evaluation/`（推理、合并、指标、工具）、`requirements.txt`、`README.md`/`README_zh.md`、`imgs/` | `ls` |
| 能力清单 | 数据转换 ✓、增强 ✓、SFT ✓、MoE 多方言 ✓、推理/评测 ✓、**GRPO ✗**、多类 SQL 模型集成 ✗ | 【README】`README.md:11-17` |
| 环境声明 | Python 3.10 + conda；CUDA 11.8–12.4 实测 | 【README】`README.md:35` |

> 说明：本快照的 HEAD 提交信息只提到 BridgeSQL，`XiYan-SQLTraining` 自身仍停留在“初版 + 未实现 GRPO”的状态；不影响本次代码级结论，但**不能把该 commit 当成 XiYan 训练框架的最新演进**（【推断】）。

## 2. 数据处理

### 2.1 两段式流程（【实现】）

```
raw_data/*.json ──data_processing.sh──> processed_data/*_nl2<dialect>.json
                                   ├──> mschema/*.json
                                   └──> configs/datasets.json（数据源注册表）
processed_data + datasets.json ──data_assembler.sh──> train/datasets/*.json（训练样本）
```

- `data/data_processing.sh:1-12`：5 个位置参数 `RAW_DATA_PATH`、`DB_CONN_CONFIG`、`PROCESSED_DATA_DIR`、`SAVE_MSCHEMA_DIR`、`SAVE_TO_CONFIGS`；默认指向 `data_warehouse/bird_train/`。
- `data/data_assembler.sh`：读 `--dataset_config_path`（即上一步产出的 `datasets.json`）、`--data_aug`、`--save_path`。

### 2.2 原始数据字段与规范化（【实现】）

`data/data_processing.py:18-57`：

| 动作 | 代码 |
|---|---|
| 库名兼容 `db_name`/`db_id` | `:23` `db_name = item['db_name'] if 'db_name' in item else item['db_id']` |
| 允许沙箱内自带 Schema 文本 | `:25` `item.get('db_schema', ...)` |
| 无 Schema 时反射真实库生成 Mschema | `:29-45` `get_db_engine(db_name, db_conn_config)` → `SchemaEngine(...).mschema.to_mschema()` |
| 可选 LLM 生成表/列注释 | `:36-42` 用 `llama_index.llms.dashscope` + `DashScopeGenerationModels.QWEN_MAX`，**`api_key='YOUR API KEY HERE.'` 硬占位** |
| 统一输出字段 | `:46-57` `{idx, db_name, question, evidence, db_schema, sql}` |
| 金标 SQL 键名 | `:56` `item.get('SQL', '')` —— 只认大写 `SQL`（BIRD 习惯），我们的 `sql` 字段需改名或加兼容 |
| 落盘 | `:73` `f"{raw_name}_nl2{db_conn_config['dialect'].lower()}.json"` |
| 注册表 | `:77-81` `data_name` 由路径 `split('/')[1:]` 拼出（**隐含 `data_warehouse/<x>/...` 两级目录前缀**），`sample_num=-1`、`sum_num=len(...)`、`task_name="nl2"+dialect` |

### 2.3 Schema 表示：M-Schema（【实现】）

格式（`data/data_utils/m_schema.py`）：

```
【DB_ID】 movie_platform
【Schema】
# Table: ratings
[
  (rating_id:INTEGER, Examples: [15610495, 10704606, 10177114]),
  (rating_url:TEXT),
  ...
]
【Foreign keys】
ratings.rating_id = ratings.rating_id
```

- 单表渲染 `m_schema.py:123-211`：`# Table: <name>, <注释>`；列行 `(<col>:<TYPE>, <注释>, <值域描述>, Examples: [...])`；`show_type_detail=False` 默认只给类型名。
- 整库渲染 `m_schema.py:212-250`：拼 `【DB_ID】`、`【Schema】`、`【Foreign keys】`，支持 `selected_tables` 过滤（`:226`，但训练流水线未启用）。
- `Examples` 来自真实库采样：`schema_engine.py:443` `select distinct <col> from <table> where <col> is not null limit 5`，最多 5 个不同值；对 PG 大表是**全表 distinct 开销**，且**可能把生产数据值带进 prompt**（见 §7 风险）。
- 表/列名引用按方言加引号（`schema_engine.py:367-373`）：MySQL/SQLite 用反引号，PG 用 `"schema"."table"` 或 `"table"` —— 双引号在 PG 中**大小写敏感**，若 01/02 的列名有大小写偏差会直接报“列不存在”。
- 反向解析 `scm_text2dict`（`m_schema.py:426-471`）把同一段文本拆回结构，供增强算子使用。

### 2.4 问题/SQL 对与 Prompt 模板（【实现】）

- 样本结构：`{"id": int, "conversations": [{"role":"user","content":prompt}, {"role":"assistant","content":sql}], "sql_type": "nl2sqlite"}`（`prompt_utils.py:198-211`）。
- Prompt 模板（`prompt_utils.py:16-92`：SQLite/MySQL/PostgreSQL/Cypher/nGQL 各一份，仅首句主语不同；另有通用 `NL2SQL_TEMPLATE:1-14`、自纠错与多候选模板 `:94-165`）：
  - 首句：`你是一名<方言>专家…生成sql语句回答【用户问题】。`
  - 结构：`【用户问题】` → `【数据库schema】` → `【参考信息】`（=BIRD `evidence`）→ **再次** `【用户问题】` → 结尾 ```` ```sql ````。
  - 即**问题出现两次**（开头 + 末尾），末尾以未闭合的 ```` ```sql ```` 收口，训练目标是裸 SQL（无 fence、无解释）。
- 组装入口 `gen_train_prompt`（`prompt_utils.py:171-213`）：`task_type` 决定模板，`"nl2postgresql"` 命中 PG 模板（`:182-183`）。
  - **缺陷**：`"nl2sqlite"` 分支写了两次（`:181` 与 `:184`），第二处本应是 `"nl2mysql"`；结果是 **MySQL 模板不可达、`nl2mysql` 任务会抛 `ValueError`**。PG 分支不受影响。
- 实测样本：`train/datasets/train_examples.json` 共 **221 条**（BIRD train 子集），`sql_type` 全为 `nl2sqlite`；user 内容字符长度 min 1131 / 中位 2675 / max 3682。

### 2.5 数据增强（【实现】）

`data_assembler.py:23-28` 固定组合：

| 算子 | 行为 | 位置 |
|---|---|---|
| `SchemaShuffle(tab_rand_p=0.5, col_rand_p=0.3)` | 打乱表顺序/列顺序；`fk` 也有概率重排 | `schema_aug.py:4-48` |
| `SchemaFilter(tab_rand_p=0.8, col_rand_p=0.7)` | 以金标 SQL 中出现的表/列为“相关”，按概率**丢弃无关表/列** —— 事实上的弱 schema linking 增强 | `schema_aug.py:51-99` |
| `SchemaPermute()` | 变换 `【用户问题】/【参考信息】` 等段落在 prompt 中的前后次序 | `schema_aug.py:101-142` |
| `SQLTranslate()` | **只做 SQL 清洗**：去注释、去换行、`extract_sql_from_strs` 抽 SQL；**不做“方言翻译”**（名不副实） | `schema_aug.py:187-215` |

### 2.6 过滤 / 去重 / 难度 / 方言（现状）

| 维度 | 仓库现状 | 证据 |
|---|---|---|
| 去重 | 无 | 全目录无 dedup 逻辑（`grep -rni dedup` 无命中） |
| 难度分层 | 无 | 同上 |
| 质量过滤 | 无（SQL 可执行性、结果非空均不校验） | `data_processing.py` 无执行步骤 |
| 采样 | 仅 `sample_num>0` 时 `random.sample`，用于多源配比 | `data_assembler.py:39` |
| 长度过滤 | `token_sample=False` 时整段关闭；打开则按 `LONG_MAX_LEN=12288` 丢弃 | `data_assembler.py:50-60` |
| train/val 切分 | 无；训练脚本 `--eval_strategy "no"` | `xiyan_sft.sh:36` |
| 方言 | 由 `db_conn_config.dialect` 单值决定整批数据方言 | `data_processing.py:73-79` |

## 3. SFT

### 3.1 框架与入口（【实现】）

| 项 | 内容 | 证据 |
|---|---|---|
| Trainer | 自研 `DeepCustomTrainer(Trainer)` | `train/sft4xiyan.py:13`、`train/trainer/trainer.py:92` |
| 入口 | `sft4xiyan.py`，`accelerate launch --config_file config/zero2.yaml` | `xiyan_sft.sh:23-24` |
| 底座默认 | `Qwen/Qwen2.5-Coder-7B-Instruct`（仅脚本变量） | `xiyan_sft.sh:70` |
| 加载 | `AutoModelForCausalLM.from_pretrained(..., torch_dtype=bfloat16, trust_remote_code=True, attn_implementation=flash_attention_2)` | `train/trainer/train_util.py:115-124` |
| 并行 | DeepSpeed ZeRO-2，8 进程 | `train/config/zero2.yaml:1,12` |
| 显存假设 | **README 未给显存数字**；8 卡 A100/H800 级为社区常规用法（【推断】） | — |
| LoRA | 可选，默认关；脚本给 `LORA_R=512`、`LORA_SCALE=1`（即 `alpha=512`） | `xiyan_sft.sh:77-79` |

### 3.2 模板与特殊 token（【实现】）

- 完全依赖模型自带 chat template：`tokenizer.apply_chat_template(conversations, tokenize=False, add_generation_prompt=False)`（`sft4xiyan.py:46-51`）。
- 不注册、不新增 special token；`tokenizer` 加载时 `padding_side="right"`、`model_max_length=args.model_max_length`、`pad_token` 缺省回退 `eos_token`（`train_util.py:104-112`）。
- 因此**换 Base 模型时第一个断点**：Base 无 `chat_template`，`apply_chat_template` 直接抛错（【实现】+ 通用 HF 行为）。评测端还传了 Qwen3 专属参数 `enable_thinking=False`（`sql_infer.py:103`），说明推理代码后来适配过 Qwen3，但**训练侧从未处理 thinking 段**（【推断】）。

### 3.3 序列构造、打包与 loss（【实现】）

- 不做 packing：`DataCollatorForGeneration` 仅做 padding 到 batch 内最长（`train_util.py:15-46`）；`group_by_length=True` 只影响取样排序（`xiyan_sft.sh:85`）。
- 长度：`tokenizer(text, truncation=True)`，上限为 `--model_max_length`（脚本 10240，类默认 8192，`argument.py:33-37`）。
- loss：标准 causal LM 交叉熵；把 prompt 段全部置 `-100`，**只对 SQL 段计 loss**（`sft4xiyan.py:56-68`）：
  ```
  labels = encodings['input_ids'].copy()
  target_idx = encodings.char_to_token(text.find(target))
  if target_idx: labels[:target_idx] = [-100]*target_idx
  ```
- **定位缺陷**（“静默错训”）：若 schema 很长导致 SQL 被截断，`char_to_token` 返回 `None` → `if target_idx:` 为假 → **完全不打 mask，prompt 也计入 loss**；若 SQL 片段在 schema 文本中更早出现，`text.find` 会定位到错误位置。我们若复用，必须改成“按 prompt 渲染后的 token 长度精确切分”，并对超长样本显式丢弃。
- 评测闭环默认关闭：脚本传了 `--predict_with_generate True`，但 `--eval_strategy "no"`，训练过程不产出任何指标（`xiyan_sft.sh:36,51`）。

### 3.4 超参与训练配方（【实现】，`xiyan_sft.sh:70-85`）

| 参数 | 值 |
|---|---|
| epochs | 5 |
| lr | 1e-6 |
| scheduler | cosine + warmup_ratio 0.1 |
| weight_decay | 0.1；AdamW，`adam_beta2=0.95` |
| batch | per_device 1 × grad_accum 2 × 8 GPU = 16 |
| max_length | 10240 |
| 其他 | gradient_checkpointing True、bf16、save_only_model True、save_steps 500、logging_steps 1、report_to none |

## 4. 评测

### 4.1 三段流水线（【实现】）

```
sql_infer.py（模型生成 pred json） → sql_eval.py（合并 pred / gt / exec 结果）
                                   → eval_utils/sql_calc_metric.py（算 EX）
```

- `sql_infer.py:97-104`：`conversations = item['conversations'][:1]`，即**测试集里已放好完整 XiYan prompt 文本**，推理侧只用模板补 chat 包装。
- `sql_infer.py:110-119`：`model.generate(..., max_new_tokens=10)` —— **只允许生成 10 个 token**，绝大多数真实 SQL 会被截断（`sql_infer.py:114`）。
- `sql_infer.py:141-143`：`inference_accelerator` 里 `read_json(...)[:40]`，默认只跑前 40 条。
- `sql_eval.py:10-25`：按索引 `zip` 合并三份等长数据，断言长度相等（`:11`）。

### 4.2 指标与比较口径（【实现】）

| 指标 | 定义 | 位置 |
|---|---|---|
| `ex_eq` | `result_eq(gt, pred, order_matters=False)`：行数、列数一致；列置换后逐行比对，行序无关 | `sql_calc_metric.py:17-24`；`value_match.py:109+` |
| `ex_bird` | `set(pred_res) == set(gt_res)`：行集合严格相等（列顺序固定） | `sql_calc_metric.py:25` |
| `executable` | 预测 SQL 是否可执行（异常记为不可执行） | `sql_calc_metric.py:52`、`:108-111` |
| SQL 文本相似 | `sql_compare()` 基于 sqlglot 存在，但**未进入指标计算** | `value_match.py:14+`；`sql_calc_metric.py` 未调用 |

- 执行：每样本新建连接（`sql_utils.py:39-57`），`multiprocessing` 16 进程、单条 `30s` 超时（`sql_calc_metric.py:57,90`）；PG 走 `psycopg2`（`sql_utils.py:53-54`）。
- **安全面**：`execute_sql` 拿到连接后直接用 `cursor.execute` 跑预测 SQL，**无只读事务、无 `statement_timeout`、无语句类型校验**（`sql_utils.py:60-71`）；对比之下 `SchemaEngine.fetch` 才用 `engine.begin()`（`schema_engine.py:297-307`）。模型输出 `DELETE/UPDATE/DDL` 会被真的执行。
- 结果后处理：`package_sqls(..., post_process=False)` 默认**不抽取** ``` 包裹或解释文本（`sql_utils.py:89,105-106`），模型多吐一个字符就判为不可执行。

### 4.3 测试集与 Schema 链接（现状）

- 测试集要求：`conversations[0].content` 为完整 prompt、`sql`/`SQL` 为金标、`db_id`/`db_name` 指向库（`sql_utils.py:95-111`）。
- 仓库**未提供** `bird_dev_mschema_0926_short.json` 及其生成脚本（`sql_infer.py:212`、`sql_eval.sh:8` 只给默认路径）；`_short` 命名说明上游做过 schema 裁剪/压缩（【推断】）。
- 结论：**本框架不含 schema 链接模块**，eval-time 的“库表裁剪”在仓库外完成；训练期的 `SchemaFilter` 只是增强，不构成链接器。这一点对我们（01 有 6 视图/52 列，02 草案）反而是可接受前提：我们本来就只喂获准 Schema 子集。

## 5. 依赖与资源

`requirements.txt:1-23`（【实现】）：

| 依赖 | 版本 | 本项目影响 |
|---|---|---|
| torch / torchvision / torchaudio | 2.3.1 / 0.18.1 / 2.3.1 | 可随镜像调整 |
| transformers | **4.42.3** | **不支持 Qwen3**（需 ≥4.51），必须升级 |
| deepspeed | 0.12.0 | 单卡用不上；升级后与新版 transformers 的兼容需测 |
| peft | 0.11.1 | LoRA 需同步升级 |
| accelerate | 0.31.0 | 与新版 transformers 配套升级 |
| flash_attn | 2.5.9.post1 | 可选；单卡可关（`--use_flash_attention False`） |
| datasets | 2.18.0 | 可用 |
| sqlglot | 26.16.2 | 仅评测/对比使用 |
| psycopg2-binary | 2.9.9 | PG 连接必需 |
| SQLAlchemy | 2.0.16 | mschema 反射 |
| llama-index | 0.11.12 | **仅 LLM 生成注释用**，我们不需要，可删（重依赖） |
| swanlab / modelscope | 0.6.0 / 1.26.0 | 可视化/下载，可删 |
| textdistance、mysql-connector、pandas、numpy、protobuf | — | 非必需 |

## 6. 复用清单

| 可复用点 | 落点 | 备注 |
|---|---|---|
| M-Schema 文本表示 + 每列 Examples（≤5 值） | 我们的 Schema 序列化格式（01/02 契约视图） | 需白名单列、去 PII、去 `phone/address/cost_*` 等 validator 禁用列 |
| 两段式数据构造（raw→processed→assembled） | `data/` 目录结构与脚本切分 | 可保留“注册表 + 装配”思路，内容换我们的契约 |
| Prompt 模板骨架（问题→schema→参考信息→问题→结尾 sql 围栏） | 生成侧 prompt | 我们的“参考信息”位放指标口径/参数说明，而非 BIRD evidence |
| `SchemaShuffle` / `SchemaFilter` | 训练增强，提升对 schema 顺序/冗余的鲁棒性 | 需限定在“获准 Schema 子集”内做，不能删到缺表 |
| `result_eq(order_matters=False)` | 结果集比对（列置换/行序无关） | 仅作辅助指标；主指标必须是契约口径 |
| `executable` 率 + 30s 超时 + 多进程执行 | 评测器的“可执行性”子项 | 必须加只读事务 |
| `sql_compare`（sqlglot AST 比对） | 诊断用（不进入分数） | 可解释“为什么错” |
| 方言模板与连接器分支 | PG 支持 | PG 分支已存在，需实测 |

## 7. 修改清单（复用前必须改）

| # | 问题 | 位置 | 改法 |
|---|---|---|---|
| M1 | `transformers==4.42.3` 不认 Qwen3 | `requirements.txt:7` | 升到 ≥4.51（建议 4.5x 最新稳定），同步 peft/accelerate/deepspeed |
| M2 | Base 模型无 `chat_template`，`apply_chat_template` 抛错 | `sft4xiyan.py:46`、`sql_infer.py:98` | 自写 Jinja 模板或直接字符串拼接；显式定义 BOS/EOS/PAD 与 `add_generation_prompt` 行为 |
| M3 | 训练目标定位靠 `text.find` + `char_to_token`，截断即静默不 mask | `sft4xiyan.py:52-68` | 先渲染 prompt 再 tokenize 得 `prompt_len`，`labels[:prompt_len]=-100`；超长样本显式丢弃并计数 |
| M4 | 评测 `max_new_tokens=10`、`[:40]` | `sql_infer.py:114,143` | 改 512–1024；去掉 40 条截断 |
| M5 | 预测 SQL 直接在库上执行、无只读/超时 | `sql_utils.py:60-71` | `SET TRANSACTION READ ONLY` + `statement_timeout` + 语句白名单（只允许单条 SELECT）；最好先在 validator 里过 |
| M6 | 评测不抽取 markdown 代码围栏里的 SQL | `sql_utils.py:89,105-106` | 打开 `post_process`，或生成后统一 `extract_sql_from_strs` |
| M7 | 单卡放不下 8B + 10240 全参 | `xiyan_sft.sh:75,77` | LoRA/QLoRA + gradient_checkpointing + max_len 压到 ~3–4k（我们 Schema 小） |
| M8 | 金标只认大写 `SQL` | `data_processing.py:56` | 兼容 `sql`/`SQL` |
| M9 | 方言专属 `nl2sqlite` 分支重复、MySQL 模板不可达 | `prompt_utils.py:181-185` | 我们只走 PG，避开；如复用需修 `nl2mysql` |
| M10 | `data_name` 依赖 `data_warehouse/<x>/...` 两级前缀 | `data_processing.py:77` | 换成显式 `--name` 参数 |
| M11 | mschema 采样 `select distinct ... limit 5` 可能取到 PII/敏感列 | `schema_engine.py:443` | 列白名单 + 值类型过滤（不采样自由文本/PII 列） |
| M12 | 执行阶段无 `db_id` 校验，缺库时报错即不可执行 | `sql_utils.py:94-102` | 前置校验库名 ∈ 允许集合 |

## 8. 不采用与风险

**不采用**

1. **GRPO 相关一切**：`XiYan-SQLTraining` 没有。同仓 `BridgeSQL/` 下有另一套：`BridgeSQL/training/rlvr/train.sh:86` 走 `--rlhf_type grpo`（ms-swift 语法），奖励插件 `BridgeSQL/training/reward_utils/plugin.py:4` 的 `sql_acc` 依赖 `sqlite_server.py:6` 的执行服务；属另一子项目、独立依赖栈，**不在本任务范围**，若后续要 GRPO 应单独立项调研（【推断】该奖励服务只支持 SQLite，PG 需自建 server）。
2. **BIRD 数据与其评测集**：与本项目业务口径无关，且会引入上游许可问题。
3. **LLM 生成表/列注释**（llama-index + DashScope）：需要付费 API、且会把注释写进 prompt，与“获准 Schema 自带注释”冲突。
4. **swanlab / modelscope / flash_attn**：非必需，减少镜像风险。
5. **`ex_bird` 严格 set 比较**：我们的金额/时间类型在 psycopg2 下返回 `Decimal`/`datetime`，跨方言/精度的严格相等会误判。

**风险**

| 风险 | 说明 | 缓解 |
|---|---|---|
| 许可 | 代码 Apache-2.0 可商用；仓库内 `data/data_warehouse/bird_train/raw_data/train.json` 为 BIRD 子集，上游许可（CC BY-SA 4.0，【推断】）可能传染数据文件 | 不复制其数据；只借鉴代码，保留 NOTICE |
| 依赖 | `transformers 4.42.3` 与 Qwen3 硬冲突；`llama-index` 引入大量传递依赖 | 最小化依赖，锁新版 |
| 安全 | 预测 SQL 直接执行，无只读/超时 | M5；评测库用只读账号 + 独立实例 |
| 隐私 | mschema 的 Examples 来自真实数据，01 含手机号/地址类列 | M11；只对枚举/数值列采样 |
| 复现性 | 无评测集、无结果、无显存说明；README 未给任何指标，其数字无法复现 | 不引用其结论；自建评测集与算力记录 |
| 方言 | PG 路径只在代码里存在，未见 PG 评测样例 | 先小样本自测 `nl2postgresql` 全链路 |

## 9. 待核实项

1. `transformers 4.42.3` 下 `group_by_length=True` 但数据集无 `length` 列时的实际行为（可能退化为随机采样）—— 需读该版本 `Trainer._get_train_sampler` 或实测。
2. `bird_dev_mschema_0926_short.json` 的构造方式：是否做过 schema linking/裁剪、列裁剪粒度 —— 决定我们“只喂契约子集”是否与上游假设一致。
3. Qwen3-8B-Base 的单卡显存实测：LoRA r=16/32 + max_len 4096 + bs 1 的峰值显存（仓库无数据）。
4. Qwen3 chat template 在“训练时 `add_generation_prompt=False`”下是否会带入 thinking 段（需读模板本身）。
5. `SchemaFilter` 的正则按 `\b` 匹配表/列名，我们的 `order_id` / `orders.id` 这类命名是否会被误删或误留 —— 需用真实契约 Schema 跑一遍人工抽查。
6. `SchemaEngine` 对 PG 视图（01 的 6 个 view）的反射是否正常（`information_schema` vs `pg_catalog`），以及 `Examples` 采样在视图上的开销。

## 10. 证据索引

| 结论 | 位置 |
|---|---|
| 无 GRPO | `README.md:15`；`requirements.txt` 无 trl |
| 数据格式 | `data/data_processing.py:46-57`；`data/data_utils/prompt_utils.py:171-213` |
| 方言模板 | `data/data_utils/prompt_utils.py:46-59`（PG） |
| mschema 采样 | `data/data_utils/schema_engine.py:441-455` |
| 增强 | `data/data_assembler.py:23-28`；`data/data_utils/aug_ops/schema_aug.py:4-215` |
| SFT 超参 | `train/xiyan_sft.sh:70-85` |
| 8 卡 ZeRO-2 | `train/config/zero2.yaml:12` |
| loss mask 逻辑 | `train/sft4xiyan.py:52-68` |
| 评测生成缺陷 | `evaluation/sql_infer.py:114,143` |
| EX 口径 | `evaluation/eval_utils/sql_calc_metric.py:17-27`；`value_match.py:109+` |
| 执行安全 | `evaluation/eval_utils/sql_utils.py:60-71` |
| 依赖 | `requirements.txt:1-23` |
| 许可 | 仓库根 `LICENSE`（Apache-2.0） |
