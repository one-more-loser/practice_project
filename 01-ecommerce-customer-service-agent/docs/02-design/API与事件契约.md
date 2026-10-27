# API与事件契约

版本：API-CS-v0.1/EVT-CS-v0.1，2026-10-22；REQ-CS-v0.2与DESIGN-CS-v0.1已批准。后续B0仅实现认证/本人输入读取及基础错误信封，见[实施记录](../04-implementation/README.md)；其余HTTP/实时及模型接口仍属待实现基线。02/03共同契约仍未获对方批准，见[跨项目契约草案](跨项目契约草案.md)。

## 1. 通用身份、格式与错误

同源浏览器建议使用服务端会话Cookie，生产安全属性与本地验证差异在后续环境评审确定；本稿不创建认证代码。服务端从会话取得subject/store/有效角色，用户参数不提供权限。非只读请求校验CSRF，员工当前角色也须是服务端授权持有的角色。模型密钥只在后端安全配置，不能由浏览器请求透传。

公开接口仅公开投影；`/me`只读本人；`/staff`核验有效分派或本店主管能力；`/shop`仅本店店铺管理员；`/knowledge`仅知识管理员；`/experience`案例Skill发布限主管。个人完整收货字段只在专用本人接口，其他响应、错误、事件和导出均脱敏。无权和不存在的个人资源对外统一404，避免存在性泄露；401表示无有效会话，403用于角色/操作能力不足而不暴露个人资源内容。

JSON使用UTF-8；时间ISO 8601带时区，业务解释Asia/Shanghai；金额结构为currency和amount_minor，后者使用分单位十进制整数字符串，避免浏览器浮点及大整数精度丢失；数量为正整数。未知字段/枚举和缺必填字段按契约拒绝，不静默补业务事实。列表默认20、最多50行，允许游标分页，每页重新鉴权；时间查询默认近30日、跨度≤366日，开始含/结束不含。

```json
{
  "contract_version": "API-CS-v0.1",
  "request_id": "req_sim_01",
  "data": {
    "operation_id": "op_sim_01",
    "status": "succeeded",
    "business_ref": "topup_sim_01",
    "money": {"currency": "CNY", "amount_minor": "10000"}
  }
}
```

```json
{
  "contract_version": "API-CS-v0.1",
  "request_id": "req_sim_02",
  "error": {
    "code": "state_conflict",
    "message": "订单已发货，无法修改地址",
    "retryable": false,
    "operation_id": "op_sim_02",
    "details": {"allowed_next": ["query_logistics", "create_transport_incident"]}
  }
}
```

| HTTP/状态 | 错误或结果类别 | 客户端/Agent处理 |
| --- | --- | --- |
| 200/201 | 已完成读取/创建或原请求结果 | 以返回业务状态/版本更新视图，不把网络完成等同资金成功 |
| 202 | accepted/processing/awaiting_agent_review/result_unknown | 用operation/run查询或订阅事件；不新建第二笔意图、不自动重复扣/退 |
| 400/422 | invalid_input/unsupported_version/contract_error | 修正字段或协议；不在错误中回显完整PII、SQL或凭据 |
| 401/403/404 | unauthenticated/forbidden/not_found | 重新登录或提示不可访问；换账号清本地上下文，不能换ID穷举 |
| 409 | state_conflict/version_conflict/idempotency_conflict | 重新读取事实；同幂等键异参数拒绝；不能无提示改金额/订单后复用旧确认 |
| 410 | confirmation_expired/history_reset_required | 重新提案确认或获取获准历史快照；旧确认不复活 |
| 429/503/504 | rate_limited/unavailable/timeout | 只读按预算有限重试；写先查operation，服务端已提交不再做第二次效果 |

所有写请求携带客户端稳定`Idempotency-Key`；作用域绑定可信主体/本店/动作/资源，服务端保存规范化参数摘要。业务版本通过`expected_version`核验。键相同参数相同返回原结果，异参数409；至少24小时直接查结果，之后业务唯一关联仍有效。HTTP状态和业务状态分别表示，未知结果不能用failed代替。

## 2. 确认、执行与简单充值

| 契约 | 输入要点 | 输出/行为 |
| --- | --- | --- |
| POST /api/v1/action-proposals | action、resource_ref、参数（SKU/数量、quote_ref、shipping_input_ref等）、expected_version | 返回confirmation_id、参数/量额摘要、事实版本、有效期5分钟和allowed_next；不执行写入，不把模型意图当顾客确认 |
| POST /api/v1/confirmations/{id}/submit | decision=confirm/cancel、Idempotency-Key | 校验本人/原摘要/有效期/当前状态；确认后创建唯一operation并执行或进入审核任务。取消确认不改订单，不耗成功改址次数 |
| GET /api/v1/operations/{id} | 本人或该业务获准员工；不可依ID猜权限 | status、原business_ref、核对结果/失败原因和可行下一步；只返回获准字段 |
| POST /api/v1/me/topups | 正amount_minor、currency=CNY、Idempotency-Key | 本人输入金额并点击提交即确认；事务内等额入账/唯一流水，返回余额及operation；不走提案/独立弹窗/审批 |
| GET /api/v1/me/wallet/entries | 分页/类型/时间范围 | 本人成功流水、金额/方向/前后余额、来源和状态；无任意编辑余额入口 |

action白名单包括order.create、order.pay、order.cancel、order.address.change、order.receipt.confirm、aftersale.apply、transport.incident.create、transport.refund.request、ticket.create。员工发货/核验/审核等使用第4节专用能力契约，不把一个通用execute工具开放给模型或顾客。Agent工具通过同一应用服务提案/执行，不能绕过确认；按键点选、金额/数量变更都与新摘要绑定。

普通已付未发货取消即使由订单页发起，也必须执行Agent资格审核：submit后可返回202/awaiting_agent_review，内部建立action类型任务；无需伪造一段顾客聊天。Agent读取真实订单/付款/发货及可退额→记录通过/拒绝/需人工依据→业务服务重新检查→取消退余额。需要人工仅补事实再审，不授权人工跳过Agent审核。待付关闭无付款退款，仍记录顾客明确确认与结果。

```json
{
  "action": "order.address.change",
  "resource_ref": "order_sim_1001",
  "parameters": {"shipping_input_ref": "shipping_sim_03"},
  "expected_version": 4
}
```

```json
{
  "amount_minor": "10000",
  "currency": "CNY"
}
```

示例均为合成标识，非真实请求。shipping_input_ref由本人受控表单生成并绑定当前主体/店铺，不允许传他人引用；服务端从引用取地址，模型和通用日志不接触完整值。直接页面来源动作可在人工接管期间由顾客操作；会话来源动作还需run/mode_epoch合法。来源类别由服务端入口/执行主体绑定，不能相信客户端或模型传source=page来绕过暂停。

## 3. 商城、本人交易与履约查询

| 接口族 | 输入与输出 | 权限/状态与拒绝 |
| --- | --- | --- |
| GET /api/v1/products；GET /products/{id} | 公开列表/商品SKU说明、当前公开价格/可售量、as_of/版本 | 游客可读；当前数值来自业务服务，不由RAG缓存确定 |
| POST/DELETE /api/v1/session；GET /session | 预置账号登录/退出、当前主体和有效角色/CSRF凭据 | 不在V1增加注册/复杂组织系统；密码/Cookie不写追踪；退出撤销恢复动作资格 |
| GET /api/v1/me/cart；PUT/DELETE /me/cart/items/{id} | 本人SKU/数量/选中状态及版本 | 改购物车不预留库存；非法SKU、下架和无权条目拒绝 |
| POST /api/v1/me/quotes | 已选购物车条目或单SKU数量；shipping_input_ref | 返回业务重算金额/运费/优惠及分摊、政策版本、有效期；变价/缺货使原报价不能执行 |
| POST /api/v1/me/shipping-inputs | 本人表单的收货人/电话/省市区/街道门牌、用途/关联资源 | 仅个人业务路径处理完整资料，返回引用及本人受控预览；请求体不进访问日志/模型；不新增通讯录产品 |
| GET /api/v1/me/orders；GET /me/orders/{id} | 订单投影、原实付/已退/净实付、状态轴/版本、允许动作 | 仅本人；列表聚合不重复明细总额；客服工具使用脱敏DTO，个人页详情完整收货DTO独立 |
| GET /api/v1/me/orders/{id}/shipments | 包裹明细映射、承运轨迹/签收、更新时间、本人receipt | 轨迹陈旧/空/超时区分；签收不代本人确认 |
| GET /api/v1/me/orders/{id}/aftersale-eligibility | 当前receipt/适用政策、可退量额/窗口、缺失事实和允许选项 | 仅说明，不自动申请/批准；运输中指向独立异常工单，正式售后关闭 |
| GET /api/v1/me/wallet | 余额/CNY、账户状态/版本、simulated=true | 只本人；客服只能通过受控工具查本人脱敏事实，不开放员工任意改余额 |

order.create提交提案绑定quote和shipping_input；order.pay绑定应付/版本；order.cancel绑定订单和Agent审核；改址绑定新引用且有效未发货/count=0；receipt.confirm只允许本人整单操作。接口返回的allowed_actions只是当前提示，执行仍复核。运输异常/部分售后不改变整单发货/整单收货范围。

## 4. 售后、工单、店铺与知识能力

| 接口族 | 输入/输出及状态门槛 | 角色与拒绝 |
| --- | --- | --- |
| GET /api/v1/me/aftersales；GET /me/aftersales/{id} | 本人申请、明细/数量/分摊金额、审核/退货/核验/退款进展 | 正式申请通过action提案；须本人确认收货及适用政策，顾客不能自填退款额 |
| POST /api/v1/me/aftersales/{id}/return-shipment | 顾客退货承运标识/单号、申请版本 | 已审核允许退货；填单号不入账，不能改审核数量/退款额 |
| POST /api/v1/staff/aftersales/{id}/reviews | approve/reject/request_info、理由/证据/版本 | 仅有效分派客服；主管争议处理走exception-decisions；Agent/未分派拒绝 |
| POST /api/v1/staff/cases/{id}/exception-decisions | 类型、核验依据、规则例外/争议原因及批准范围 | 本店主管；不超原顾客/剩余可退额，不直接修改余额 |
| POST /api/v1/shop/aftersales/{id}/return-inspections | 实收/可售明细数量、证据引用、版本 | 本店店铺管理员；满足审批及实收后触发唯一退款任务，数量不符转争议，不凭按钮虚构证据 |
| POST /api/v1/me/transport-incidents；GET /me/transport-incidents/{id} | transport.incident.create的有效确认/operation、订单/包裹与查询/催件/拦截/丢件诉求；返回独立工单与状态 | 运输中本人资源；与确认提交复用同一应用服务和operation，不重复建工单；退款诉求另经明确确认，不能因此开放正式售后 |
| POST /api/v1/staff/transport-incidents/{id}/reviews | 分派审核、拦截/责任核验材料、结论/版本 | 获分派客服或主管例外；未核实退回/丢件责任不能退款；店铺退回核验使用shop能力 |
| POST /api/v1/shop/transport-incidents/{id}/return-inspections | 实际退回明细、数量/可售状况、证据/版本 | 店铺管理员仅核验物流退回，不代替客服审批或直接改余额 |
| GET /api/v1/me/tickets；GET /me/tickets/{id} | 本人工单公开进展/回复 | 不含内部备注；新建通过ticket.create确认，重放不重复建单 |
| POST /api/v1/me/tickets/{id}/reopen | 仍未解决理由/原版本 | 关闭7日内重开；超期另建关联工单，不静默删除历史 |
| GET /api/v1/staff/queue；GET /staff/tickets/{id} | 分派前脱敏摘要；获准后关联必要详情 | 客服只有效分派；主管本店队列；均不能反查全店个人资料 |
| POST /api/v1/staff/tickets/{id}/assignments | 本店负责人、理由、expected_version | 主管分派/改派；接单只允许已分派客服；旧分派权限立即失效 |
| POST /api/v1/staff/tickets/{id}/actions | accept/reply/note/resolve/close/escalate、内容/原因/版本 | 校验具体能力与状态，note不发顾客；普通/高优先级首响和升级留时钟 |
| POST/PATCH /api/v1/shop/products 或 /shop/skus/{id} | 本店商品/SKU/售价/上下架及expected_version | 店铺管理员；版本冲突重读，不把02候选价格当已执行 |
| POST /api/v1/shop/skus/{id}/inventory-adjustments | 数量方向、业务原因/引用、expected_version | 管理员；不得低于预留量，流水可对账；不增加仓库系统 |
| POST /api/v1/shop/orders/{id}/shipments | 整单明细、模拟承运标识、版本、Idempotency-Key | 管理员且paid/unshipped；成功事务定义发货边界，拒绝部分发货和终态 |
| GET/POST/PATCH /api/v1/knowledge/units 或 /faqs及其详情 | 本店列表/版本详情，受控文本/问答、来源版本/范围/有效期、synthetic和状态 | 知识管理员；最小文本维护，不接受任意批量/OCR/订单CSV导入 |
| POST /api/v1/knowledge/{kind}/{id}/reviews 或 /publications | 审核结论/范围与来源版本；发布/停用/版本 | 知识管理员单级审核发布；索引未就绪不能假报新知识可检索 |
| POST /api/v1/experience/cases/candidates 或 /skills/candidates | 已处理脱敏来源、名称/条件/步骤/排除项；生成候选任务 | 自动候选不生效；发起归纳限主管，Agent可推荐，后台核验来源/脱敏 |
| POST /api/v1/experience/{kind}/{id}/reviews 或 /publications | 主管审核、Skill回归证据版本、发布/停用/回退 | 主管；无回归通过不能发Skill，不能增加工具/权限或覆盖退款审批规则 |

退款执行没有面向顾客/模型的“任意金额入账”端点。内部RefundService仅消费已批准申请、核验和固定本人账户，持久operation后由同一业务事务执行/核对；人工接口的approve也不能直接把余额改成目标数。资料/证据引用须由本项目获准对象提供，不接受外部任意URL让模型自主访问。

表内省略的/api/v1前缀沿用同一根路径，kind等枚举分别校验，不开放任意资源路由。案例/Skill列表和版本详情限主管获准读面；Agent获取适用片段走内部experience工具，不把内部Skill全文作为顾客下载接口。核验证据采用已授权业务/模拟轨迹及文字事实引用，不因此新增附件上传平台。

## 5. 会话与任务HTTP契约

| 接口 | 输入/输出 | 行为 |
| --- | --- | --- |
| POST /api/v1/conversations；GET /conversations/{id} | 创建/读取获准会话、mode/epoch/版本、公开历史游标 | 游客仅公开会话；登录创建本人会话；账号切换不自动并入游客或他人历史 |
| POST /api/v1/conversations/{id}/messages | client_message_id、脱敏前受控输入、可选reply_to | 入站隐私处理后短事务保存消息/run/task并202确认；同ID同内容返原接收结果，异内容409；不等模型完成才接收 |
| GET /api/v1/conversations/{id}/messages | before/after游标、limit | 获准持久消息及最终/被替代状态，不能从历史恢复越权内部备注 |
| POST /api/v1/conversations/{id}/human-requests | 明确申请/原因、expected_epoch | 进入queued即暂停Agent，创建/关联工单，不显示已接通；无人在线保留待处理 |
| POST /api/v1/staff/conversations/{id}/takeover | 有效assignment、expected_epoch/版本 | 仅获准客服接管；主管先按规则分派/改派；成功后human |
| POST /api/v1/staff/conversations/{id}/end-service | 结束原因/结论、assignment/expected_epoch | 自动转agent并递增epoch，顾客不用点恢复；不关闭顾客会话、不重放旧写动作 |
| GET /api/v1/runs/{id}；POST /runs/{id}/cancel | 获准run，取消原因 | 展示执行/中断/未知结果；取消停止未提交步骤，已成功业务仍显示原事实，不能当订单取消 |
| POST /api/v1/conversations/{id}/close | 明确关闭会话及当前版本 | 关会话不删除历史，也不代替售后/工单关闭；后续新问题创建新会话 |

## 6. SSE、WebSocket和历史补偿

### 6.1 持久事件信封

公共会话流与员工内部流分离，各有stream_id/seq。seq在所属流锁定递增并与事件同事务提交，不能分配后未提交却让后续游标跳过。服务端接收ACK意味着已持久化，不等于用户已读；客户端去重用event_id，补偿用流内seq，业务去重仍用operation_id。

```json
{
  "contract_version": "EVT-CS-v0.1",
  "event_id": "evt_sim_037",
  "stream_id": "stream_public_sim_01",
  "seq": 37,
  "conversation_id": "conversation_sim_01",
  "mode_epoch": 8,
  "type": "run.completed",
  "occurred_at": "2026-10-22T10:30:00+08:00",
  "data": {"run_id": "run_sim_04", "message_id": "message_sim_20", "status": "completed"}
}
```

事件类别：message.accepted、run.started、run.progress、answer.delta、answer.completed、answer.superseded、confirmation.required、operation.updated、service.mode_changed、ticket.updated、run.failed/cancelled。只有提交后的事实才能发operation succeeded；令牌增量不是订单事件。事实未核验前只发进度，不流出确定的金额/状态再尾部修正。

### 6.2 SSE

GET `/api/v1/conversations/{id}/events?after_seq=...`，同源Cookie鉴权且绑定获准公共流，响应text/event-stream。SSE `id`使用该流的seq，`event`为type，`data`为上述信封；客户端不能拿员工流游标请求公共内容。浏览器重连优先使用Last-Event-ID，查询参数只作首次初值，避免重连头与旧URL参数冲突。

建议15秒无业务事件发送心跳，心跳不作为持久业务消息，不推进业务seq；重连退避1/2/4/8秒、上限30秒并加抖动，45秒无心跳时断开重连。这些是待审传输初值，需真实浏览器/代理验证。MDN说明SSE包含id/retry及重连机制，但业务去重/历史补偿仍由本项目定义。[SSE官方参考](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)

断流后run继续。重连先查当前身份/mode_epoch，再按最后获准seq补事件，去重后应用；过旧游标已清理则history_reset_required，获取获准消息快照和新游标，不伪造增量或重新执行旧写动作。未来游标/格式错拒绝，不无限等待。新模式事件使旧epoch增量失效，已显示片段可标“已转人工”，迟到内容不覆盖人工会话。

### 6.3 人工WebSocket

连接 `/api/v1/realtime`，仅同源Cookie＋Origin白名单。建立后5秒内发送hello，含会话绑定CSRF及请求订阅的流/游标；服务端鉴权成功才返回ready和任何正文。禁止将会话/模型密钥放URL。之后支持subscribe/resume、staff.reply、delivery.ack、ping/pong；业务资金/审核仍走受控HTTP服务。

staff.reply含client_message_id、conversation_id、assignment_version、expected_epoch及正文；与HTTP回复使用同一消息接收服务，先核验有效分派再持久化，返回包含message_id/seq的ACK后广播。ACK丢失以原client_message_id重试，不生成重复回复。delivery.ack只是已收位置，不表述顾客已读。

心跳/重连初值同SSE；断线只改变连接状态，不自动结束人工。每次投递前重新核验角色/分派/epoch，撤销时停止订阅并清除前端详情；不能让旧订阅持续读。客服结束服务生成service.mode_changed(agent,new_epoch)，顾客自动由Agent处理新诉求；不依赖顾客选择恢复。

### 6.4 跨进程及竞争

每个API进程只持有本地连接和短期游标，定期从持久流读取。NOTIFY可唤醒轮询，但其丢失、监听中断或API重启都由历史游标补齐，不把通知当唯一来源。事件、消息、run/operation关联保存在PostgreSQL，投递是至少一次，客户端/消费者需去重，不宣称网络恰好一次。

会话排队/接管/结束与Agent事件/动作提交争用同一会话门槛，检查expected_epoch和有效任务栅栏。不得仅在任务开始检查一次：模型返回时已进入human，旧回复/写动作必须拒绝。原业务已成功时保留事实并通知当前服务方，不能因展示模式变化撤销资金。

## 7. 契约验证与变更

设计评审核对：鉴权负例、同键异参、5分钟确认、跨24小时重放、改址/发货、取消/发货、付款/超时、人工epoch竞争、WS重放、SSE补偿和脱敏链路。JSON示例只作静态契约核对，不代表替身或服务运行通过。后续锁版本后生成OpenAPI/Schema、前后端类型和契约测试；当前不生成实现资产。

添加可选字段须验证旧消费者；删除字段、改变金额/状态/权限或错误语义视为破坏性变更，升级契约并评审受影响FR/BR/AC和共同消费者。个人字段不能以向后兼容名义新增到公开/员工投影。共享契约获批前仅运行内部替身计划，不能称02/03已联调。
