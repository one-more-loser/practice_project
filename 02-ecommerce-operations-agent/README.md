# 电商促销筹备与经营分析 Agent

面向店铺管理人员的经营工作台：经营快照 → 确定性指标与方案测算 → 人工确认 → 受控活动执行 → 实际回传与监控。

## 工程

- backend/：Python3.12、FastAPI、SQLAlchemy，独立PostgreSQL库；tests保留业务和协议测试。
- frontend/：Vue3、TypeScript、Vite，资源base为/operations/。
- contracts/：协议与快照约束；scripts/：本地与集成入口。

## 文档

- [需求规格](docs/01-requirements/PRD.md)、[追踪与验收](docs/01-requirements/需求追踪与验收矩阵.md)。
- [V1闭环设计](docs/02-design/V1闭环设计.md)、[跨项目接口](docs/02-design/跨项目接口与01变更单.md)。
- [检查状态](../docs/STATUS.md)、[环境与运行前提](../docs/GETTING_STARTED.md)。

## 当前范围

源码和R6恢复准备产物已独立核对。R6恢复在额度中断后续接，前检因01依赖离线封存为blocked，02启动0次、PG/HTTP0次；授权有效，等待01恢复及统筹新复核引用。历史真实快照问数、方案及有限活动回传证据不能替代当前运行态。

最小SQL为operations_store_min_sqlite / sqlite_exact_integer_sum_v1，覆盖支付金额、支付订单数、指定SKU支付件数，在明确归档快照上隔离执行。当前规则问数不等于03模型已接入，和03 PostgreSQL训练协议仍需适配。

金额、利润、预算、库存和产能由业务代码计算，缺失事实标记未知。集成模式依赖01在线身份、店铺能力和受控活动接口，不自动建库、不提供demo管理员身份。依赖见backend/requirements.lock及frontend/package-lock.json。

旧集成脚本绑定历史run；不能重放已提交活动或已ACK事件，也不能对现有业务库重跑初始化。逐次本机验收归档不随公开仓库上传，当前公开范围见根目录docs。
