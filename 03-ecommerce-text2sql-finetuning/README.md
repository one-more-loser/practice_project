# 电商 Text-to-SQL 微调工程

面向电商业务的只读SQL候选、澄清与不支持响应。底座固定Qwen/Qwen3-8B-Base，训练路线SFT → GRPO；训练、推理和隔离评测代码已提供，**真实8B训练尚未进行**。

## 工程

- training/：原生PyTorch SFT/GRPO、LoRA、断点恢复、推理与同题评测。
- sandbox/：独立PostgreSQL评测、fixture、runner与SQL校验；不连接01/02业务库。
- data/sft_v1/：冻结的152条train、12条dev、20条test合成样本、manifest及审核侧车。
- scripts/：数据与工程工具；docs/：需求、设计和数据说明。

## 文档

- [训练与配置](training/README.md)、[样本来源及边界](data/sft_v1/README.md)。
- [需求规格](docs/01-requirements/PRD.md)、[数据适配方案](docs/02-design/数据适配方案.md)。
- [三方契约差异](docs/02-design/三方契约差异表.md)、[质量验收方案](docs/02-design/质量验收方案.md)。
- [当前检查状态](../docs/STATUS.md)、[仓库收录规则](../docs/REPOSITORY_LAYOUT.md)。

## 验证范围

有限真实PG生命周期及本地工程检查已有证据；不证明完整奖励评测、8B收敛、GPU兼容性、生产稳定性或业务收益。RTX PRO 6000 96GB配置是待目标环境验证的训练方案；没有本次真实模型权重或GPU训练结果。

模型不持有业务数据库凭据或执行权，权限和最终SQL校验由01/02消费端实施。02当前最小SQLite协议与本工程customer_self PostgreSQL协议不直接兼容。

公开仓库保留源码、fixture及冻结小样本；原始外部数据、生成中间集、outputs、模型、虚拟环境、PGDATA和本机验收档案仅留本地。训练费用、模型准备、CUDA短步验证和正式训练须另行安排；本次上传不执行训练。
