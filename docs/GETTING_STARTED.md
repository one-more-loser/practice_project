# 环境与运行说明

## 环境隔离

| 工程 | 环境与依赖入口 |
|---|---|
| 01后端 | Python3.12，backend/pyproject.toml 与 backend/uv.lock |
| 01前端 | Vue3/TypeScript/Vite，frontend/package-lock.json；Node版本须符合package.json的engines |
| 02后端 | Python3.12，backend/requirements.lock |
| 02前端 | Vue3/TypeScript/Vite，frontend/package-lock.json |
| 03评测 | sandbox/requirements.txt；独立PostgreSQL沙箱及其fixture |
| 03训练 | training/requirements-native.txt、独立Linux/GPU环境；见training/README.md |

安装操作由使用者在自己的新环境执行。本次上传没有重新安装依赖或运行训练，也不把依赖声明当作运行通过。

## 01与02

01的数据库与模型配置入口见 backend/src/cs_agent/config.py；数据库部署模板在 deploy/。需要PostgreSQL/pgvector、专库和独立角色，必须核对迁移与角色配置。不能把本机历史集成脚本直接用于新目标库。

02提供独立demo入口与integration入口；集成模式依赖01的在线身份和受控活动接口。配置使用OPS_DATABASE_URL、OPS_IDENTITY_BASE_URL、OPS_TRADE_BASE_URL、OPS_ACTIVITY_SERVICE_TOKEN、OPS_SERVICE_STORES等环境变量，秘密只在后端提供。真实模式只校验Schema，不把serve当自动建库命令。

本地演示、业务集成和生产部署应分别配置。先核对进程/监听归属，再使用各项目scripts中适合当前环境的入口。旧脚本中带固定run、端口或身份的集成入口用于历史验证，不是通用一键启动器。

## 模型与03

模型接口的地址、密钥、模型名及实际能力分别配置。01历史主客服/临时SQL仅允许deepseek-flash；Embedding/重排使用Qwen路线。当前上传快照不附带这些服务的密钥、地址配置或权重，默认不启动模型。

03先读 [训练说明](../03-ecommerce-text2sql-finetuning/training/README.md)与 [样本说明](../03-ecommerce-text2sql-finetuning/data/sft_v1/README.md)。准备GPU、模型与训练费用后再执行训练。合成数据和tiny工程测试不证明真实8B收敛。

## 验证与提交

保留各项目独立测试入口。只有环境条件满足时才执行真实PG/模型/集成测试；无法执行的项标记未运行，不能用单测通过代替。

Git提交前运行根目录scripts/check_repository.py，检查暂存文件、敏感字符串和大文件。它是发布检查，不是全面安全审计，也不会连接数据库、发送模型请求或启动服务。
