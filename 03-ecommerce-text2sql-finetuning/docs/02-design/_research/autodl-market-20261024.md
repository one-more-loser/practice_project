# AutoDL 市场核对（快照 2026-10-24）

| 项 | 内容 |
|---|---|
| 文档状态 | 只读调研快照；不构成显卡选型结论、预算或租卡批准 |
| 抓取者 | 子代理 C（只读价格核对） |
| 抓取窗口 | 2026-10-24 11:34:30–11:38:50 +08:00（Asia/Shanghai） |
| 目标页面 | `https://www.autodl.com/market/list`（HTTP 200，3,177 字节） |
| 平台版本 | `v6.73.0`，BuiltTime `2026-10-22/18:11:06`（`GET /api/v1/version`） |
| 账号状态 | 未登录、未租卡、未充值、未调用付费 API |
| 一句话结论 | 匿名只能取得「机型目录（含显存）+ 区域清单 + 官方计费规则」；**单价、库存、区域分布被登录墙挡住**（`POST /api/v1/machine/search` 返回 `AuthorizeFailed`）。本文件不填任何价格数字。 |

> 证据口径：区分「事实（抓取结果 / 文档原文）」与「推断（我的判断）」。价格为易变事实，未取得即写“未取得”，不估造、不用常识补。

## 1. 抓取记录

### 1.1 成功抓取（全部 HTTP 200，匿名、无 Cookie）

| # | 时间 (+08:00) | 方式 | URL | 字节 | 结果摘要 |
|---|---|---|---|---|---|
| 1 | 11:34:30 | GET | `https://www.autodl.com/market/list` | 3,177 | SPA 外壳，`<div id="app">`，无任何价格文本；`last-modified: Thu, 22 Oct 2026 10:11:08 GMT`，`content-type: text/html` |
| 2 | 11:38:48 | POST | `https://www.autodl.com/api/v1/machine/search` | 125 | `{"code":"AuthorizeFailed","msg":"登录超时，请重新登录"}`；`x-request-id: ac457e01da384619d080e8bc2c664ca9` |
| 3 | 11:38:49 | GET | `https://www.autodl.com/api/v1/machine/gpu_type` | 9,498 | 机型目录 33 条（含显存、架构、算力排名），**无价格字段** |
| 4 | 11:38:49 | GET | `https://www.autodl.com/api/v1/region/list` | 2,873 | 区域 19 个（北京/西北/内蒙/重庆/佛山/上海） |
| 5 | 11:38:49 | GET | `https://www.autodl.com/api/v1/machine/tag` | 895 | 标签 7 个，含 `1元GPU`、`极致性价比` |
| 6 | 11:38:50 | GET | `https://www.autodl.com/api/v1/common` | 3,065 | 全站公告：西北B区上线 1000 卡 `vGPU-32G(RTX 4080S 32G)`、10 月下旬 400 卡 `PRO 6000` |
| 7 | 11:38:50 | GET | `https://www.autodl.com/api/v1/version` | 140 | `v6.73.0`，`BuiltTime 2026-10-22/18:11:06` |
| 8 | 11:38:50 | GET | `https://www.autodl.com/docs/price/` | 66,887 | 计费规则全文；**“各型号GPU价格”章节只写“请以网站显示的现价为准”** |
| 9 | 11:35:xx | GET | `https://www.autodl.com/assets/index.ec54d3c7.js` | 10,855 | 市场列表页前端；`charge_type` 枚举与筛选字段（见 §3.5） |

辅助文档页（同窗口抓取，均 HTTP 200，用于 §2.4–§2.6 的计费/准入事实）：`/docs/local_disk/`、`/docs/netdisk/`、`/docs/student/`、`/docs/member/`、`/docs/real_name_cert/`、`/docs/service_agreement/`、`/docs/pay_later_agreements/`、`/docs/agreements/`、`/docs/qa/`、`/docs/gpu/`。

### 1.2 受限 / 失败尝试（用于证明“不是没试”）

| 尝试 | 结果 | 说明 |
|---|---|---|
| `POST /api/v1/machine/detail`（`{"id":1}`） | `AuthorizeFailed`，request_id `f61c6c3b503c16c364aa510ad6bc177f` | 机器详情需登录 |
| `POST /api/v1/machine/simple/detail`（`{"id":26}`） | `AuthorizeFailed`，request_id `57f56e9841998f0e41e270333e8aeab1` | 同上 |
| `POST /api/v1/machine/check_machine_online` | `AuthorizeFailed`，request_id `dbaacb22cbd734bf21df8a4863cc469e` | 库存/在线状态需登录 |
| `POST /api/v1/machine/region/gpu_type` | `AuthorizeFailed`，request_id `0872b1f5d05f57d32a3fd9e7c883274e` | 区域×卡型分布需登录 |
| `GET /api/v1/machine/search` | `404 page not found` | 该接口只接受 POST |
| `GET /api/v1/machine/list`、`/api/v1/market/list`、`/api/v1/machine/region/list` | `404 page not found` | 不存在这些路径 |
| 先 `GET /` 建 Cookie 会话，再 POST search | 仍 `AuthorizeFailed` | 匿名访问不发放会话 Cookie |
| 爬虫 UA（Baiduspider / Googlebot）请求 `/market/list` | 仍 3,177 字节，HTML 中 `4090` 出现 0 次 | 无 SSR/预渲染 |
| `https://m.autodl.com/market/list`、`https://h5.autodl.com/market/list` | `404 nginx`（自签名证书） | 无移动端站点 |
| `https://www.gpuhub.com/market/list` | 首页 200，但 POST 其 `/api/v1/machine/search` TLS 失败（`SSL_ERROR_SYSCALL`） | 关联站点不可作为替代证据 |
| `https://api.autodl.com/api/v1/machine/search` | `AuthorizeFailed` | 与主站同一鉴权 |

## 2. 在售卡型与价格

### 2.1 三条结论先写清楚

1. **价格：未取得。** 市场列表的价格/库存由 `machine/search` 返回，匿名请求被 `AuthorizeFailed` 拒绝；官方文档明确不列价（§3.7）。本文任何卡型都不写单价。
2. **库存/区域分布：未取得。** 区域清单公开（19 个），但“哪个区域有哪种卡、几张可租”需登录。
3. **机型目录：可证。** `machine/gpu_type` 返回 33 个 SKU，可确认“平台是否提供某型号/显存”，但不能等同于“当前有货”。

### 2.2 目标卡型核对表

| 目标卡型 | 目录内对应机型（id） | 单卡显存 | 计费单价（元/卡/时） | 区域/库存 | 结论 |
|---|---|---|---|---|---|
| RTX 4090 24G | `RTX 4090`(26)、`RTX 4090D`(34) | 24GB | 未取得（需登录） | 未取得（需登录） | 在册 |
| RTX 3090 24G | `RTX 3090`(1) | 24GB | 未取得（需登录） | 未取得（需登录） | 在册 |
| L20 48G | 无 | — | — | — | **目录无 L20** |
| L40S 48G | 无 | — | — | — | **目录无 L40S** |
| A100 40G | `A100-PCIE-40GB`(25) | 40GB | 未取得（需登录） | 未取得（需登录） | 在册（仅 PCIE 40G 版） |
| A100 80G | 无 | — | — | — | **目录无 A100 80G**（80GB 档为 A800/H800，见 §2.3） |
| H100 80G | 无 | — | — | — | **目录无 H100**（80GB 档为 H800） |

> 口径提醒：上表“在册”仅表示机型目录存在该 SKU（`gpu_type` 含 `created_at/updated_at`），**不等于当前可租、也不等于有目标区域库存**。`RTX 4090D` 与 `RTX 4090` 是两条独立 SKU，显存同为 24GB。

### 2.3 目录内的 40G/48G/80G/96G 备选（同窗口可证）

| 机型（id） | 显存 | 架构 | 与目标卡型的关系 |
|---|---|---|---|
| `A100-PCIE-40GB`(25) | 40GB | Ampere | A100 40G 对应项 |
| `vGPU-48GB`(44)、`vGPU-48GB-350W`(50)、`vGPU-48GB-425W`(48) | 48GB | Ada | 48G 档；目录未标物理型号（**推断**：虚拟化切片卡，非 L20/L40S） |
| `A800-80GB`(37)、`A800-80GB-NVLink`(51) | 80GB | Ampere | A100 80G 的合规替代 |
| `H800`(41) | 80GB | Ada | H100 80G 的合规替代 |
| `H20-NVLink`(42) | 96GB | Ada | 大显存档 |
| `RTX PRO 6000`(49)、`PRO 6000 Max-Q`(53) | 96GB | Blackwell | 大显存档 |
| `RTX 6000D`(52) | 84GB | Blackwell | 大显存档 |
| `RTX PRO 5000`(54) | 72GB | Blackwell | 中大显存档 |
| `MTT S4000`(46) | 48GB | 国产（摩尔线程） | 需框架适配，**推断**不作首选 |

> `vGPU-32GB`(39) 按全站公告即 `RTX 4080S 32G`（`/api/v1/common` 原文：`上新通知: 西北B区即将陆续上线1000卡vGPU-32G(RTX 4080S 32G)，10月下旬将上线400卡PRO 6000`）。`vGPU-48GB` 的单精标称 82.58 TFLOPS 与 RTX 4090 目录值一致，但平台未标注物理卡，**此为推断，需登录后核对**。

### 2.4 计费口径（官方文档可证）

| 计费方式 | 口径 | 证据 |
|---|---|---|
| 按量计费 | 开机开始计费、关机结束计费；`费用 = 时长 × 单价`，精确到秒，最低 0.01 元；整点扣费一次 + 关机扣费一次 | `/docs/price/` §按量计费 |
| 包年包月 | 预付费；支持按日/周/月/年；**租期内无论是否开关机都计时**；到期关停，可设“到期自动转按量” | `/docs/price/` §包年包月 |
| 计费周期枚举 | `["payg","daily","weekly","monthly","yearly"]`，单位 `["/时","/日","/周","/月","/年"]` | 前端 `assets/index.ec54d3c7.js` |
| 折扣 | 存在“会员价/包月优惠”，但**折扣率与折后单价均未公布** | `/docs/member/`、`/docs/agreements/` 3.2.2 |

> 因此“单价（元/卡/时）”只能来自登录后的市场页或下单页；公开面没有任何一个可核对的数字。

### 2.5 存储/镜像/网盘计费（与训练成本直接相关）

| 项 | 规则 | 单价 | 证据 |
|---|---|---|---|
| 数据盘 | 默认免费 50GB；超出部分按当日最大容量每日结算，**无论实例是否开机** | 约 0.0066 元/日/GB（会员价，原文“以实际网站显示的价格为准”） | `/docs/local_disk/` |
| 系统盘 | 默认基础 30GB；按日计费，无论是否开机；扩容另计 | 30GB 基础容量 0.10 元/日；扩容价以网页为准 | `/docs/price/` §系统盘计费 |
| 文件存储 | 20GB 以下免费，超出按当日最大容量 | 0.01 元/GB/日 | `/docs/price/` §文件存储计费 |
| 公网网盘 | 免费 20GB，按月预付 | 0.30 元/GB/月 | `/docs/price/` §网盘计费 |
| 镜像 | 30GB 以下免费 | 0.01 元/GB/日 | `/docs/price/` §镜像计费 |

> 推算（**推断**，仅用于量级）：若数据盘扩容 50GB，按文档会员价约 0.33 元/日 ≈ 9.9 元/月。实际以登录后单价为准。

### 2.6 准入、优惠、关机（只写页面能证实的）

| 问题 | 页面证据 | 结论 |
|---|---|---|
| 是否要求实名 | `/docs/real_name_cert/`：“如您勾选本说明，您可继续购买并使用需要实名认证的产品，如您拒绝勾选同意，不会影响您浏览网页了解我们的服务”；`/docs/service_agreement/`：“甲方通过实名认证，方可使用乙方提供的自定义服务功能” | 实名通道存在且有产品要求实名；**“租 GPU 必须先实名”未见明文**，需登录下单验证 |
| 是否有保证金 | 在已抓取的 18 个官方文档页中未检索到“保证金/押金”字样 | 未发现保证金条款；**不能据此断言不存在** |
| 先用后付/授信 | `/docs/pay_later_agreements/`：“本功能不向学生（包含博士生）开放”；“只能使用您自己的实名账号申请授信” | 存在授信制（非保证金），学生不可用 |
| 学生优惠 | `/docs/student/`：“学生认证进入会员快速升级通道，优享会员价”，单次认证一年有效；`/docs/member/`：“认证学生期间一直为炼丹会员”，权益为“GPU会员优享价” | 有学生通道，**但折后价未公布** |
| 新用户优惠 | `/docs/member/`：“当前活动默认注册后会送1个月炼丹会员” | 有，但同样不公布折后价 |
| 关机是否收费 | `/docs/price/` 按量计费：“关机结束计费”；同页系统盘/数据盘章节：“无论实例是否开机，均会计费” | 关机停 GPU 计费；**存储类（系统盘/数据盘/镜像）继续按日计** |
| 库存/可租卡数 | 市场页筛选字段含 `gpu_idle_num`（按空闲卡数过滤）、`pay_price_order`（按价格排序） | 页面支持按库存/价格筛选，但数据需登录 |

## 3. 证据片段（原文，勿改写）

### 3.1 市场页 HTML 外壳（3,177 字节，`2026-10-24 11:34:30 +08:00`）

```html
<!DOCTYPE html>
<html lang="zh">
  <head>
    <meta charset="UTF-8" />
    <title>AutoDL算力云 | 弹性、好用、省钱，GPU算力零售价格新标杆</title>
    ...
  </head>
  <body>
    <div id="app"></div>
    ...
    <noscript>
      <div ...>AutoDL网站需要使用JavaScript实现功能，您的浏览器禁用了JavaScript权限，请打开权限以继续。</div>
    </noscript>
```

### 3.2 价格接口的登录墙（`2026-10-24 11:38:48 +08:00`）

```json
{"code":"AuthorizeFailed","data":null,"msg":"登录超时，请重新登录","request_id":"ac457e01da384619d080e8bc2c664ca9"}
```

请求体（与前端市场列表页一致）：

```json
{"charge_type":"payg","region_sign_list":[],"gpu_type_name":"","machine_tag_name":[],
 "gpu_idle_num":0,"mount_net_disk":false,"cpu_arch":[],"chip_corp":[],
 "page_index":1,"page_size":10,"default_order":true}
```

### 3.3 机型目录（`GET /api/v1/machine/gpu_type`，节选）

```json
{"id":26,"gpu_name":"RTX 4090","gpu_series_type":"NVIDIA-Ada","gpu_memory":25769803776,
 "floating_point_hash_rate":"单精 82.58 TFLOPS / 半精 165.2 Tensor TFLOPS","gpu_rank":200,"chip_corp":"nvidia"}
{"id":1,"gpu_name":"RTX 3090","gpu_series_type":"NVIDIA-Ampere","gpu_memory":25769803776,"gpu_rank":150}
{"id":25,"gpu_name":"A100-PCIE-40GB","gpu_memory":42949672960,"gpu_rank":132}
{"id":44,"gpu_name":"vGPU-48GB","gpu_memory":51539607552,"gpu_rank":19198}
{"id":41,"gpu_name":"H800","gpu_memory":85899345920,"gpu_rank":2000}
{"id":37,"gpu_name":"A800-80GB","gpu_memory":85899345920,"gpu_rank":131}
{"id":42,"gpu_name":"H20-NVLink","gpu_memory":103079215104,"gpu_rank":10000}
```

目录共 33 条，全量见附录 A。**该接口无 `price` 字段。**

### 3.4 区域与标签

- 区域 19 个：`west-B/west-C/west-D/west-E`（西北B区）、`bj-B1/bj-B2/bj-B3`（北京B区）、`cq-A1`（重庆A区）、`nm-B1/nm-B2`（内蒙B区）、`beijing-A/beijing-B/beijing-C/beijing-D/beijing-E`（北京A区/V100专区）、`foshan-A`（佛山区）、`nm-A1`（A800专区）、`west-X`（西北企业区）、`sh-A1`（摩尔线程专区）。
- 标签：`通用型`、`大显存`、`高主频CPU`、`多核心CPU`、`1元GPU`、`自主可控`、`极致性价比`。

### 3.5 前端计费枚举（`https://www.autodl.com/assets/index.ec54d3c7.js`）

```js
const e=["payg","daily","weekly","monthly","yearly"].indexOf(i.filter.charge_type);
return{index:e,unit:["/时","/日","/周","/月","/年"][e]}
```

同一文件的市场筛选字段（证明“库存/价格排序”是页面能力，但数据被鉴权）：

```js
{charge_type, region_sign_list, gpu_type_name, machine_tag_name, gpu_idle_num,
 mount_net_disk, cpu_arch, chip_corp, page_index, page_size,
 pay_price_order, gpu_idle_type, default_order}
```

### 3.6 平台版本与公告

```json
{"BuiltTime":"2026-10-22/18:11:06","GitCommit":"tags/v6.73.0-0-gf161a5615","Version":"v6.0.0"}
{"global_notice":[{"content":"上新通知: 西北B区即将陆续上线1000卡vGPU-32G(RTX 4080S 32G)，10月下旬将上线400卡PRO 6000"}]}
```

### 3.7 官方文档为什么不给价

`/docs/price/` 两处“各型号GPU价格”章节的正文均为：

> 各型号GPU价格 ¶ 请以网站显示的现价为准

`/docs/member/` 的会员权益同样写：

> GPU会员优享价 ¶ 具体价格请以网站实时显示价格为准~

## 4. 页面限制

1. **纯前端渲染**：`/market/list` 只有 3,177 字节外壳，价格由 JS 调 `machine/search` 后渲染；无 SSR，Googlebot/Baiduspider UA 拿到同样外壳。
2. **数据接口全量鉴权**：`search`、`detail`、`simple/detail`、`check_machine_online`、`region/gpu_type` 一律 `AuthorizeFailed`；匿名访问不发放会话 Cookie，带 Cookie 重试仍失败。
3. **文档刻意不列价**：`/docs/price/` 与 `/docs/member/` 均把价格指向“网站实时显示”，无法作为价格证据。
4. **公开接口只给目录**：`gpu_type` / `region/list` / `machine/tag` / `common` 可匿名读取，但都不含价格与库存。
5. **无移动端/替代域名**：`m.autodl.com`、`h5.autodl.com` 为 404；`gpuhub.com` 接口 TLS 不通；`api.autodl.com` 同样鉴权。
6. **前端资源可作旁证**：`assets/index.ec54d3c7.js` 公开可读，只能证明“有按日/周/月/年计费和按库存/价格筛选的能力”，不能证明任何具体价格。

## 5. 对 8B QLoRA / LoRA / GRPO 的初步适配备注

> 本节只是**适配备注**，不是选型结论；最终选型见 `docs/02-design/显卡选型建议.md`，且需价格与实测支撑。可行性判断与 PRD 附录 C（QLoRA 24GB 起、LoRA bf16 48–80GB、GRPO 48–80GB 需实测）保持一致。

| 显存档 | 目录内可选 | QLoRA 4bit（8B） | LoRA bf16（8B） | GRPO（8B，含 rollout） | 备注（推断） |
|---|---|---|---|---|---|
| 24GB | RTX 4090 / 4090D / 3090 | 可行但紧 | 不可行 | 不推荐 | 需 gradient checkpointing + 小 seq/batch；4090 比 3090 快，但两者都受 24GB 限制 |
| 32GB | RTX 5090、vGPU-32GB（4080S 32G） | 可行 | 边缘 | 不推荐 | 32G 对 QLoRA 更从容，仍不够 LoRA bf16 |
| 40GB | A100-PCIE-40GB | 舒适 | 边缘可行 | 需实测（训练+rollout 共存难） | A100 显存带宽优于消费卡 |
| 48GB | vGPU-48GB 系列、MTT S4000 | 舒适 | 可行 | 小规模可行 | 目录无 L20/L40S；MTT 为国产卡，需框架适配，不作首选 |
| 72–96GB | RTX PRO 5000/6000、6000D、H20-NVLink、A800-80GB、H800 | 宽裕 | 稳定 | 可行（可含 vLLM rollout） | 80G 档需注意 A800/H800 为合规型号，非 A100/H100 原版 |

补充备注：

- **24GB 是 QLoRA 的下限而非舒适区**：8B 4bit 权重约 5GB，剩余显存要同时容纳激活、LoRA 优化器状态与 SQL 长上下文；seq 与 batch 必须压小。
- **GRPO 与 SFT 的资源结构不同**：GRPO 要“策略模型 + 参考模型 + 采样/rollout”，24GB 基本放不下；48GB 起才有讨论空间，80GB 更稳（与 PRD 附录 C 一致）。
- **48G 档在 AutoDL 目录里不是 L20/L40S**：如果方案依赖“便宜的 48G 卡”，需按 `vGPU-48GB` 重新评估（其物理卡未标注），不能按 L40S 的算力/带宽做预算。
- **磁盘会咬预算**：底座约 16GB，加数据集/checkpoint/合并产物，PRD 建议数据盘 ≥100GB；免费额度 50GB，超出按 0.0066 元/日/GB（会员价）逐日计费，**关机也计费**。
- **按量计费适合迭代**：关机即停 GPU 计费；但系统盘 0.10 元/日 + 付费数据盘每日结算，长期挂机会累积。
- **价格缺口直接卡住“首选/备选 + 总费用”**：PRD AC-SQL-012 要求单价、时长、总费用与估算依据；在拿到登录后报价前，这一条无法闭合。

## 6. 未取得项

| 未取得项 | 原因 | 复现方式（需授权） |
|---|---|---|
| 全部卡型的按量单价（元/卡/时） | `machine/search` 需登录 | 登录后打开 `https://www.autodl.com/market/list`，或抓 `machine/search` 响应 |
| 包日/包周/包月/包年单价与折扣率 | 同上 | 市场页切换 `charge_type`，或下单页费用明细 |
| 各卡型的区域分布与可租卡数 | 同上 | 市场页按区域筛选；`region/gpu_type` 需登录 |
| 会员价/学生价的具体折后金额 | 文档只写“以网站实时显示为准” | 完成学生认证后对比市场页价格 |
| 是否强制实名才能租 GPU | 文档未明说 | 登录后尝试下单，观察实名拦截 |
| 是否有保证金/押金 | 18 个文档页无此字样，但非全站证明 | 咨询客服或查看充值/下单页协议 |
| 目标卡型 L20 / L40S / A100 80G / H100 80G | 机型目录中不存在 | 若必须使用，需换平台或等平台上新 |
| 实际训练吞吐/显存占用 | 未租卡、未实测 | 租卡后跑 QLoRA/LoRA/GRPO 小试 |

## 附录 A：全量机型目录（`GET /api/v1/machine/gpu_type`，2026-10-24 11:38:49 +08:00）

| id | 机型 | 显存 | 架构 | 算力排名 | updated_at |
|---|---|---|---|---|---|
| 39 | vGPU-32GB | 32GB | NVIDIA-Ada | 29200 | 2026-08-23 |
| 45 | RTX 5090 | 32GB | NVIDIA-Blackwell | 20000 | 2025-10-20 |
| 49 | RTX PRO 6000 | 96GB | NVIDIA-Blackwell | 19202 | 2026-03-10 |
| 51 | A800-80GB-NVLink | 80GB | NVIDIA-Ampere | 19201 | 2026-03-10 |
| 52 | RTX 6000D | 84GB | NVIDIA-Blackwell | 19201 | 2026-06-08 |
| 53 | PRO 6000 Max-Q | 96GB | NVIDIA-Blackwell | 19200 | 2026-08-11 |
| 44 | vGPU-48GB | 48GB | NVIDIA-Ada | 19198 | 2025-04-07 |
| 48 | vGPU-48GB-425W | 48GB | NVIDIA-Ada | 19195 | 2025-07-02 |
| 47 | RTX 5090 D | 32GB | NVIDIA-Blackwell | 19190 | 2025-07-04 |
| 54 | RTX PRO 5000 | 72GB | NVIDIA-Blackwell | 19000 | 2026-09-30 |
| 42 | H20-NVLink | 96GB | NVIDIA-Ada | 10000 | 2024-11-08 |
| 41 | H800 | 80GB | NVIDIA-Ada | 2000 | 2024-11-25 |
| 34 | RTX 4090D | 24GB | NVIDIA-Ada | 202 | 2024-08-28 |
| 26 | RTX 4090 | 24GB | NVIDIA-Ada | 200 | 2023-04-27 |
| 1 | RTX 3090 | 24GB | NVIDIA-Ampere | 150 | 2023-02-01 |
| 12 | RTX 3080 Ti | 12GB | NVIDIA-Ampere | 149 | 2026-08-31 |
| 33 | RTX 3080x2 | 20GB | NVIDIA-Ampere | 148 | 2026-08-31 |
| 25 | A100-PCIE-40GB | 40GB | NVIDIA-Ampere | 132 | 2024-03-03 |
| 5 | V100-32GB | 32GB | NVIDIA-Volta | 131 | 2023-07-18 |
| 37 | A800-80GB | 80GB | NVIDIA-Ampere | 131 | 2024-05-31 |
| 22 | V100-SXM2-32GB | 32GB | NVIDIA-Volta | 125 | 2022-06-13 |
| 3 | RTX 2080 Ti | 11GB | NVIDIA-Turing | 93 | 2023-07-25 |
| 19 | RTX 3080 | 10GB | NVIDIA-Ampere | 92 | 2023-07-25 |
| 11 | RTX A4000 | 16GB | NVIDIA-Ampere | 90 | 2022-03-16 |
| 9 | RTX 3060 | 12GB | NVIDIA-Ampere | 80 | 2021-12-22 |
| 4 | GTX 1080 Ti | 11GB | NVIDIA-Pascal | 50 | 2022-04-01 |
| 27 | Tesla T4 | 16GB | NVIDIA-Turing | 10 | 2023-04-12 |
| 46 | MTT S4000 | 48GB | S（国产） | 10 | 2025-04-18 |
| 31 | CPU | 0GB | CPU | 2 | 2023-11-14 |
| 36 | RTX 2080 Ti x2 | 22GB | NVIDIA-Turing | 2 | 2024-05-17 |
| 40 | CPU-close-HT | 1GB | CPU | 2 | 2024-11-25 |
| 2 | TITAN Xp | 12GB | NVIDIA-Pascal | 1 | 2024-11-14 |
| 50 | vGPU-48GB-350W | 48GB | NVIDIA-Ampere | 1 | 2025-09-19 |

> 表内显存已由 JSON 的 `gpu_memory`（字节）换算为 GiB 并取整；`vGPU-48GB-350W` 的架构字段在源数据中标为 `NVIDIA-Ampere`，与其余 vGPU 的 Ada 不一致，需登录后核对。目录不含 `price` 字段。
