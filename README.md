# 电商双 Agent 与 Text-to-SQL 实践

三个独立工程组成的学习与工程实践仓库：客服处理顾客服务，运营负责经营分析与促销决策，Text-to-SQL 工程提供专项数据、训练和评测代码。

| 工程 | 职责 | 入口 |
|---|---|---|
| 01 客服与模拟商城 | 商品/政策检索、本人订单查询、余额支付、履约售后、工单与人工协同 | [01 README](01-ecommerce-customer-service-agent/README.md) |
| 02 运营工作台 | 经营快照、确定性测算、方案确认、活动执行及监控 | [02 README](02-ecommerce-operations-agent/README.md) |
| 03 Text-to-SQL | 只读 SQL 候选、数据准备、SFT → GRPO、隔离评测 | [03 README](03-ecommerce-text2sql-finetuning/README.md) |

## 当前状态

这是开发快照。01/02 已有实现和有限联调证据；03 已有训练代码和合成数据，**尚未进行真实 Qwen3-8B-Base 训练**。当前没有完整 V1、模型效果或生产验收结论。最新范围与未决项见 [检查状态](docs/STATUS.md)。

身份、权限、金额、库存与业务写操作由后端控制；模型输出不拥有数据库执行权或退款审批权。02 当前最小 SQL 协议与03的 PostgreSQL训练协议仍需适配，不能直接混用。

## 目录

```text
practice_project/
├── 01-ecommerce-customer-service-agent/
│   ├── backend/ frontend/ contracts/ data/
│   ├── deploy/ model_service/ scripts/ tests/
│   └── docs/
├── 02-ecommerce-operations-agent/
│   ├── backend/ frontend/ contracts/ scripts/
│   └── docs/
├── 03-ecommerce-text2sql-finetuning/
│   ├── data/sft_v1/ sandbox/ training/ scripts/
│   └── docs/
├── docs/                   # 跨项目说明与公开检查摘要
└── scripts/                # 仓库检查工具
```

保留工程目录名和内部布局，便于复用现有路径与契约。需求、设计、计划位于各项目 docs 的编号目录。[结构与收录规则](docs/REPOSITORY_LAYOUT.md)解释源码、样本和本地记录的边界。

## 使用

先阅读 [环境与运行说明](docs/GETTING_STARTED.md)，再按项目说明准备独立环境。仓库不包含可直接访问原演示库的凭据，也不自动启动数据库、调用模型或训练。新机器应创建自己的隔离配置与数据库；历史文档里的机器路径、端口和执行结果只代表相应时点。

提交前运行：

```bash
python scripts/check_repository.py --staged
```

本地虚拟环境、模型权重、外部大数据、数据库目录、运行凭据及逐次机器验收归档由 `.gitignore` 排除，原文件保留本机。冻结的03合成训练/开发/测试集保留在仓库，不能为了修复评测而改写测试集。
