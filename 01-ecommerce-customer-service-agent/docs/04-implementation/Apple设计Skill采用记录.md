# Apple设计Skill采用记录

2026-10-22：用户要求“前端的ui设计……github上有公开了苹果的ui设计的skill或者是mcp，你去看一下采用这个”。已检索并采用[daft2/apple-hig-skill](https://github.com/daft2/apple-hig-skill)，它是社区整理的Apple Human Interface Guidelines，MIT许可；不是Apple官方发布的Skill。

项目副本：[SKILL.md](../../.agents/skills/apple-hig/SKILL.md)；[许可](../../.agents/skills/apple-hig/LICENSE)。提交固定为`d7fb4531db34115c2dd7c2089abd3641362a8a98`。通过Skill Installer的显式项目内dest安装，只下载该技能目录并保留许可，没有改用户全局技能或MCP配置。当前会话手动读取后使用；自动技能发现如需刷新，以后续会话提供的目录清单为准，不能把文件存在称为已注册的MCP能力。

比较时发现[tmaasen/apple-dev-mcp](https://github.com/tmaasen/apple-dev-mcp)已归档；当前需要的是可审查的设计规则，不需要运行一个Apple文档服务，故选择Skill路线。[Apple官方HIG](https://developer.apple.com/design/human-interface-guidelines)作为追溯来源；具体平台规则需与当前官方资料核对。

采用重点是层级、留白、语义颜色、清晰按钮/字段、键盘操作、减少动画和信息反馈。保留已确认的暖白＋橙色、Vue3/TypeScript/Vite/Element Plus及电脑网页范围。平台专属pt值、SwiftUI、Apple Pay、Sign in with Apple、SF资产、手机适配不随Skill进入范围；不将全站做成玻璃表面。

界面上下文见[设计上下文](../../.better-web-ui.md)。本轮后续B1实现与浏览器结果另行登记；安装Skill不代表商城界面或业务验收完成。

B1应用结果：商城、购物袋、结算、余额、订单和店铺页面已按上述规则实现，类型/构建与真实浏览器局部旅程通过；[截图及范围](../06-acceptance/B1/README.md)。只核对本项目适用规则，未宣称满足全部Apple平台条款或完成完整无障碍认证。
