# 03 原生 PyTorch：SFT → GRPO

> ITER-20261025-01：本轮不训练，包含 tiny 更新与完整 pipeline。当前复核入口为 `docs/07-iteration/ITER-20261025-01/review-output/`；下文启动命令只供未来训练获准后使用，旧 42 回归和 CPU 九阶段是历史证据，不能视为本轮或修补后的数值验收。

目标是单卡 **RTX PRO 6000 Blackwell 96GB**，底座固定为 **Qwen/Qwen3-8B-Base**。
关键训练过程由原生 PyTorch 实现：forward、loss、backward、梯度累积、optimizer.step 和 GRPO 更新。
Transformers 提供模型/tokenizer/生成，datasets 可用于数据读取；LoRA 由本项目实现。
默认 BF16 LoRA、SDPA、梯度检查点，不依赖 TRL、Trainer、Accelerate、DeepSpeed、verl、PEFT 或 bitsandbytes。

当前属于工程实现与本地验证。**未租卡、未下载 8B、未真实训练、未付费**。
CPU 微型模型/数学测试不能证明 8B 收敛、Blackwell 内核兼容、96GB 容量或业务收益。

## 配置与文件

| 文件 | 用途 |
|---|---|
| `configs/rtx_pro_6000_96gb.json` | 96GB 默认参数，模型仅从本地路径读取 |
| `configs/smoke_cpu.json` | 微型本地模型的 CPU 调试起点，须本地创建 tiny 模型；不能装载 8B |
| `requirements-native.txt` | Python 3.11 / Linux x86_64 直接依赖锁定 |
| `check_environment.py` | 包版本、CUDA、BF16、架构、空闲显存、模型分片结构、磁盘、数据存在性检查 |
| `train_sft.py` / `train_grpo.py` | 原生训练，支持 `--config --set --output --resume` |
| `lora.py` / `runtime.py` / `grpo_math.py` | 原生 LoRA、参考参数、checkpoint、GRPO 数学 |
| `create_smoke_model.py` | 本地随机创建微型 Qwen3，无权重下载，仅验证流程 |
| `pipeline.py` | Base → SFT → SFT 评测 → GRPO → GRPO 评测调度 |
| `inference.py` | 同一 prompt 契约下生成候选 JSON |
| `eval_sft.py` / `eval_business.py` | 结构评测 / validator 与 PG 多 fixture 业务 oracle |

所有命令在 **03 项目根目录**执行；相对配置路径以 03 根目录解析，不改变 01/02。
参数以实际 `--help` 为准；配置/CLI 集成由本轮主代理验证，本文不把命令列出当成已运行。

## 手动准备环境

以下是后续获准使用 GPU 后的操作说明，**不会自动安装、下载或启动训练**。
使用 Python 3.11、Linux x86_64；CUDA wheel 自带运行库，驱动仍须支持 CUDA 12.8。
不要根据 `nvidia-smi` 显示的 CUDA 上限推断当前 PyTorch wheel 可用。

```bash
python3.11 -m venv .venv-native
source .venv-native/bin/activate
python -m pip install -r training/requirements-native.txt
python training/check_environment.py --config training/configs/rtx_pro_6000_96gb.json --offline --no-model
```

锁定 `torch==2.7.1+cu128`、`transformers==4.51.3`；未锁全量传递依赖与 wheel 哈希。
如镜像已有旧包，在新 venv 安装；不改 `.bashrc`、系统 Python 或全局 CUDA。
官方依据：[2.7.1 cu128 安装](https://pytorch.org/get-started/previous-versions/)、
[PyTorch 2.7 Blackwell 支持](https://pytorch.org/blog/pytorch-2-7/)、
[Transformers v4.51.3 Qwen3 源码](https://github.com/huggingface/transformers/blob/v4.51.3/src/transformers/models/qwen3/modeling_qwen3.py)。

模型未来由人工准备到配置的 `model.path`，保留 Hugging Face revision/文件哈希作为来源证据。
目录应包含 config/tokenizer 文件和 safetensors 分片/index。
检查器只读 header 验证分片存在与未截断；不读取权重 tensor，不验证上游 hash，不证明模型来源。

```bash
python training/check_environment.py --config training/configs/rtx_pro_6000_96gb.json --offline
python -m training.pipeline --config training/configs/rtx_pro_6000_96gb.json --dry-run
```

缺包/无 GPU/模型缺失时输出 JSON 并以状态码 1 结束；`--no-model` 只跳过模型检查。
`preflight_passed` 表示所执行的检查通过；`ready_for_training` 始终为 false，因为检查器不执行 CUDA forward/backward。
本地无 GPU 时也能运行并列出待补条件。

## 后续人工启动

```bash
# 单独 SFT；--set 可重复，值使用 JSON 标量
python -m training.train_sft --config training/configs/rtx_pro_6000_96gb.json \
  --set sft.max_steps=60 --output outputs/native-96gb/sft

# 从本轮 SFT adapter 开始 GRPO
python -m training.train_grpo --config training/configs/rtx_pro_6000_96gb.json \
  --sft-adapter outputs/native-96gb/sft/adapter --output outputs/native-96gb/grpo

# 完整流水线，在环境/数据/费用审批与短步检查完成后人工执行
python -m training.pipeline --config training/configs/rtx_pro_6000_96gb.json

# 恢复必须传入本阶段真实 checkpoint 路径，不能拿模型目录代替
python -m training.train_sft --config training/configs/rtx_pro_6000_96gb.json \
  --resume outputs/native-96gb/sft/checkpoint-20

python -m training.inference --config training/configs/rtx_pro_6000_96gb.json \
  --adapter outputs/native-96gb/sft/adapter --question '我有多少笔订单？'
```

配置默认四小时为整次流水线的程序预算，各阶段共享；超时终止进程组，不等于云实例自动关机。实例/存储计费由人工负责。
运行前看 dry-run 中各阶段实际路径和 argv；adapter/checkpoint 的实际命名以训练输出为准。
`pipeline --stages base sft sft_eval` 可限定阶段；完整路线保留 GRPO。
完整流水线恢复可在配置设置 `pipeline.resume.sft` / `pipeline.resume.grpo`。
SFT 显式参数 `fused_adamw`、`length_bucket_size`、`loss_chunk_tokens`、`pin_memory` 可通过 `--set` 调整；CPU 配置关闭 fused/pinned memory。
CPU 配置仍保留 1152 的 SFT 长度、1024 的 prompt 上限；8 token 生成只用于微型流程冒烟，不能据此评定 SQL 输出质量。

GRPO 使用同一冻结底座和两套小型 LoRA 参数；reference 在加载 GRPO checkpoint 前冻结为 SFT adapter。
old/current/reference 按同一 `temperature` 计算概率；本实现要求 `grpo.top_p=1`，并显式关闭 top-k，保证采样和重要性比率对应。
支持 BF16/FP32；GRPO 不支持 FP16，会明确拒绝。SFT 的 FP16 路径使用原生 GradScaler。
EOS 兼作 decoder padding；prompt 和答案超限时不静默截断。零方差奖励组记录为无学习信号，不能把 optimizer step 或权重衰减当成 GRPO 学习成功。

```bash
# 本地 CPU 流程验证，不下载底座。需要已安装 CPU torch 与本项目依赖。
python -m training.create_smoke_model --output outputs/tiny-qwen3-smoke
python -m training.pipeline --config training/configs/smoke_cpu.json
python -m unittest discover -s training/tests -p 'test_*.py' -v
```

## 数据与验收

当前 `data/sft_v1` 为已冻结的 152/12/20 条、19 训练任务组/15 种 query SQL。本轮保留数据及其历史 `candidate_unreviewed` 元数据，不重建、不扩写；先前的独立签收记录继续有效。
只使用 01 `customer_self/public` 消费面；02 经营口径不得混入此训练包。
训练/生成共用 `common.prompt()`；SFT 对答案 token 计算 CE，prompt mask 为 -100。
GRPO 的分组、奖励、reference KL、token mask、clipping 与梯度累积必须用数学测试和短步运行检查。
独立 test 使用 fx_a 的静态 expected，固定分母 12 query / 4 clarify / 4 unsupported；三阶段使用同一 oracle。
GRPO 的训练奖励在 fx_a/fx_b/fx_c 三个独立临时 PG 中执行，对比训练 gold 结果；只读 train，不把测试答案用于训练奖励。
测试前运行 `sandbox` 自身环境检查，不把模拟奖励当成 PG 验收。

本地验证记录由主代理汇总到 HANDOFF；后续 GPU 清单见 `docs/02-design/96GB原生训练运行说明.md`。
`requirements-train.txt` 兼容入口已指向 `requirements-native.txt`，避免误装旧训练框架；旧 QLoRA 首跑文档保留为历史材料。
本地证据见 `outputs/native-code-verification/`；随机小模型的业务分数不代表 8B 效果。

## 本轮修补后的文件契约

- 预测输入拒绝重复/未知/空 ID 和非字符串 generated；缺预测在原分母记零。跳过/超时/截断与超长输出不得通过协议评分；严格 JSON 拒绝重复键和 NaN/Infinity，非 query 不能夹带 SQL。
- 业务判定核对列名、列顺序及带类型的行多重集，NULL 不等于 0、bool 不等于整数。连接/环境错误使本轮作废，候选 SQL 语法/数据错误、超时或行数超限记失败。三 fixture 都空的训练 gold 标记无区分度，不发正奖。
- SFT 加载模型前检查 train/dev 显式 split、manifest hash、ID/组隔离；customer_self prompt 拒绝 generic_sqlite 和 operations profile。GRPO 的 reward.fixtures 已实际接线，至少选择两组已知独立 fixture。
- checkpoint 绑定 Base 权重/tokenizer 内容、源码、schema、manifest、数据及优化语义；读入前校验 checkpoint-integrity.json。adapter 还核对 Base 与 dropout；无法证明身份的旧产物需显式转换，不能静默补身份。历史 tiny 产物保留供查阅，不直接恢复到本轮源码。
- 每个进程加载 Base 后顺序读取权重计算一次内容 hash，不在每步重复。该磁盘开销及 GPU 峰值仍待实测；优化器步、LoRA 计算保持原生 PyTorch。
- pipeline 和直接训练分别持有输出目录锁；锁未释放时拒绝新写者。崩溃留下的锁须先核实记录的 PID/host，再由操作者处理；不自动杀未知进程。
- Base/SFT/GRPO 比较必须有同一 frozen rows + oracle/source/validator 的 evaluation_identity。旧评分报告保留，但需要显式用 raw predictions 重新评分，不能与本轮新协议分数直接比较。
- 本轮安全子集运行脚本：`sandbox/.venv/bin/python docs/07-iteration/ITER-20261025-01/review-output/evidence/safe_suite.py`。它不等于全训练测试，不执行真实 PG/模型/梯度更新。
