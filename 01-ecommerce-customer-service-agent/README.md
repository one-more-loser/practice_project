# 电商店铺客服 Agent

模拟商城与嵌入式客服：公开商品/政策检索、本人订单与物流查询、多轮澄清、余额支付、履约售后、工单与人工接管。

## 工程

- backend/：Python3.12、FastAPI、SQLAlchemy/Alembic、LangGraph/LangChain；PostgreSQL/pgvector。
- frontend/：Vue3、TypeScript、Vite；商城、嵌入式客服和工作台。
- contracts/：查询、事件及跨项目协议；data/：合成店铺样本。
- model_service/：Embedding/重排服务代码；deploy/与scripts/：部署模板和工程工具。

## 文档

- [需求规格](docs/01-requirements/PRD.md)、[验收矩阵](docs/01-requirements/需求追踪与验收矩阵.md)。
- [设计入口](docs/02-design/README.md)、[开发计划](docs/03-plan/README.md)。
- [跨项目检查状态](../docs/STATUS.md)、[环境与运行前提](../docs/GETTING_STARTED.md)。

## 当前范围

2026-10-27的R6已有服务恢复经独立进程、健康与只读数据库核验通过。有限促销联调中，两条事件已投递并ACK；仍有权限与幂等负例、模型和完整V1验收缺口。该结果不保证服务持续在线。

模型只提出候选，身份、权限、金额及业务写入由受控后端执行。客服和主管只能获得脱敏电话/地址；退款审批与人工分派遵循业务规则。运营决策属于02，SQL训练属于03。

本仓库不带真实凭据、模型权重或原演示库。本地逐次实施/HANDOFF/验收档案保留在原机器；GitHub入口以本页和根目录docs为准。历史集成脚本含固定run与目标校验，不是可直接用于新机器的一键初始化工具。
