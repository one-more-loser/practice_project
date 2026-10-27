# customer_self 查询接入实现与当前基线（2026-10-24）

> 当前验收更新：223 passed（完整tests/unit）；聚焦85 passed包含于223，不重复累加。 本地实现与回归验收通过；未签收真实PostgreSQL、真实TCP服务、03模型联调、前端呈现或生产性能。现有业务库、历史迁移、旧失败证据未改；未启动付费模型、租卡或下载8B权重。 详见[主代理独立验收](主代理独立验收.md)。下文为实施时原始交接记录。

状态：01 本地实施完成，待主代理独立验收；XCS 联合协议仍为待评审，未声称 03/真实模型联调通过。

## 当前有效查询基线入口

- `consumer-baseline.json`：从当前源码导出六视图全部52列、Schema/digest/四注册指标、23项迁移链 revision/down_revision/SHA、七项可信 GUC、参数类型与时间规则。不能只复制0011；0012已完整修复8表外层限定，本轮未新增重复迁移。现库有效策略的既有只读证据见 `../03-rls-review-20261024/`；本轮未复查现库目录。
- 角色限定customer_self，SQLExecutor每事务SET LOCAL search_path=csq,pg_catalog，以及cs.owner/store/batch/generation/role/start/end。身份从当前运行时主体绑定，不进入模型请求。订单/流水RLS防御窗闭区间；业务SQL用aware半开窗。明细/物流继承父订单created_at范围；钱包余额与商品没有自身时间窗。
- 默认RLS防御窗口仍是可信now前30天；这不是用户日期意图的替代。候选声明日期参数但用户没有确认日期时，Graph澄清，不填默认值。明确日期必须保留start/end参数，否则不执行。
- 查询角色受限、只读事务、2秒statement timeout/500ms lock timeout、既有EXPLAIN成本和行数预算保持。此次订单/SKU在候选前与执行前各做一次范围解析，不引入逐行鉴权；未测真实PG性能。

## 实现与状态

1. 服务端从当前用户原话解析日期；模型改写问题和slots提示不能覆盖用户明确年月。支持YYYY年M月/YYYY-MM、YYYY年M月D日/YYYY-MM-DD、明确双端日期含结束自然日、本月/上月/今天/昨天/近N天；业务时区Asia/Shanghai，转换UTC，0<window≤366天。未实现或混合时间、截至/之前/之后、月区间、缺年份月份、季度先澄清。未声称完整自然语言解析。
2. 订单显示号或内部ID在当前owner/store/batch/generation精确查询，最多2条以拒歧义；不存在、他人或旧批次统一not_found。SKU需当前店/批次/generation且SKU及商品active；不能接收模型虚构值。公开商品不额外按owner过滤；商品名称/多个规格当前由澄清选规范SKU，不自动猜测。
3. status按对应列枚举验值；paid只在“状态为/是/：已付款、已支付或paid”时作为单状态确认。“已支付订单数/金额”等cohort由四注册指标口径处理，不自动缩为trade_status=paid。候选不能丢弃已经确认的order/SKU/status或显式日期。
4. 保存后端私有sql_binding，含身份scope指纹、原题、历次补充、context_id/version、确认值；模型只得到确认参数名、显式时间、当前可信时钟和指标定义。scope不外发。澄清补槽自动继续SQL，原题与历次回答传给候选模型。识别不了的回答视为新话题，不承诺覆盖任意语言的意图判断。
5. 本人/角色/会话/auth_version/店/批次/generation变化使旧绑定失效；取消、新话题、成功、拒绝/异常清理。改选已有订单或SKU清掉旧资源和筛选，只保留本轮明确时间。模型等待期间重验业务范围和身份，变化fail closed。最多三轮澄清后结束，未执行业务写操作。
6. clarify保留reason_code、missing_slots、嵌套question/options、顶层questions/options及协议/版本绑定；Graph补自己的context_id/version，直接回答不再经chat改写。Runtime answer.completed及会话GET增量clarification字段保持兼容；unsupported/error保留code/error_code/error/retryable/service等字段，以query_response增量交付，不降格成澄清。自由文本仍隐私过滤；机器标识只容许长度受限ASCII，不把随机UUID误当电话号码。

## 请求/响应样例

`protocol-examples.json`给出完整合成customer_self请求、正确四已付状态SQL、结构化clarify及unsupported/error字段示意。XCS-SQL-v0.1新增可选query_context/metric_definitions；已有字段不删除。双方仍需冻结样例与服务能力，不能把01实现当作联合批准。

## 聚焦验证及复跑

```powershell
# 在01项目根目录；禁止改成tests/api或运行PG/reset fixtures。
& backend/.venv/Scripts/python.exe -m pytest backend/tests/unit/test_sql_call_chain.py backend/tests/unit/test_model_gateway.py backend/tests/unit/test_sql_validator.py -q --junitxml=docs/04-integration/query-binding-20261024/focused-unit.xml
& backend/.venv/Scripts/python.exe -m ruff check backend/src/cs_agent/query/bindings.py backend/src/cs_agent/agent/graph.py backend/src/cs_agent/models/gateway.py backend/src/cs_agent/conversation/service.py backend/src/cs_agent/runtime/leases.py backend/tests/unit/test_sql_call_chain.py
```

最终聚焦85通过（含旧28，不累加此前50/68/78批次）。纯Python/SQLite内存订单与SKU解析、真实Graph/validator方法、MockTransport网关；Executor是记录替身，绝非PG/RLS/真实模型验收。覆盖明确月份与上海跨日/闰月/年底、未实现日期不回退、多轮原题和补充、scope六项变化、选SKU失效、缺参、伪造/跨人/旧批次、候选丢条件、paid cohort、取消/新话题、结构化结果保留。SQLExecutor真实执行及RLS仍需隔离PG后续验收。

曾尝试全unit，约42项后无进展，已Ctrl+C中止exit1，不计为通过；该套含app startup等非本任务聚焦模块，后续不再运行。未运行默认PG/API/reset fixture，未迁移/seed/操作现库、未开业务服务、未调用付费模型或部署。旧证据未改写，03/02未写入。前置14:53/14:54遗留bindings/graph/tests实现已复用；本轮不是从空白重建。

## 验收边界和下一步

未验证：真实03模型/HTTP、PG执行与连接池、前端交互呈现、并发资源失效竞态/长时负载。未新增业务指标、通用查询平台或RLS迁移。≥80可执行题+≥40反例仍是01消费验收要求，不作为03开始SFT的前置阻塞；本批unit不能折算成80+40。主代理独立验收后再记录签收范围。

本环境未发现answer-me-with-html技能，保留同内容HTML作为复杂交付展示。
