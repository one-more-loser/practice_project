"""顾客可见的动态事实由业务结果生成，模型负责理解和选择工具。"""

from decimal import Decimal

from cs_agent.core.errors import AppError

TRADE = {
    "awaiting_payment": "待付款",
    "cancelled_unpaid": "未付款已取消",
    "pending_payment": "待付款",
    "paid": "已付款",
    "completed": "已完成",
    "cancelled": "已取消",
    "expired": "已过期",
    "cancelled_refunded": "已取消并退款",
    "closed_refunded": "已关闭并退款",
}
FULFILLMENT = {"unshipped": "未发货", "in_transit": "运输中", "received": "本人已确认收货",
               "received_by_customer": "本人已确认收货"}
ERRORS = {
    "customer_login_required": "请先登录，再查询本人的业务信息。",
    "not_found": "未找到可访问的记录，请从本人订单页面核对订单。",
    "invalid_tool_arguments": "查询条件不完整或格式不正确，请补充后重试。",
}


def money(minor):
    return f"{Decimal(str(minor)) / 100:.2f}元"


def select_policy_evidence(results, references=None):
    """模型只选择已授权的条款版本；同版本命中的条件/例外一并保留。"""
    available = {
        item["citation"]["reference_id"]
        for tool in results if tool["name"] == "knowledge.search"
        for item in tool["result"].get("items", [])
    }
    if not available:
        return results, []
    if references is None:
        # 兼容单一来源的旧响应；多来源缺少选择时不能把所有片段倒给顾客。
        if len(available) != 1:
            return None, []
        selected = available
    else:
        if (not isinstance(references, list) or len(references) > 5
                or any(not isinstance(ref, str) for ref in references)
                or not set(references) <= available):
            raise AppError("agent_protocol_error", 503, "客服证据选择不符合契约")
        selected = set(references)
        if not selected:
            return None, []
    filtered, citations = [], []
    for tool in results:
        if tool["name"] != "knowledge.search":
            filtered.append(tool)
            continue
        items = [item for item in tool["result"].get("items", [])
                 if item["citation"]["reference_id"] in selected]
        if items:
            filtered.append({**tool, "result": {**tool["result"], "items": items}})
            citations.extend(item["citation"] for item in items)
    return filtered, citations


def render_facts(results):
    """不把模型的数字/状态描述拼接进权威事实；空结果与失败也有明确答复。"""
    output = []
    for tool in results:
        name, result = tool["name"], tool["result"]
        if result.get("error"):
            output.append(ERRORS.get(result["error"], "本次查询未能完成，请稍后重试。"))
        elif name == "catalog.read":
            if not result.get("items"):
                output.append("没有找到匹配的在售商品，请核对商品名称或SKU。")
            for item in result.get("items", [])[:10]:
                for sku in item["skus"][:5]:
                    output.append(
                        f"{item['name']}（{sku['sku_id']}）：当前售价{money(sku['price_minor'])}，"
                        f"可售库存{sku['available_qty']}件。下单时以重新核对的价格和库存为准。"
                    )
        elif name == "action.propose":
            output.append(
                "已准备操作摘要，请核对下方确认卡。确认前不会执行业务操作；退款仍须满足审核与到账条件。"
            )
        elif name in {"aftersales.list", "transport.list", "tickets.list"}:
            labels = {
                "reviewing": "待审核",
                "awaiting_return": "待退回",
                "return_in_transit": "退货运输中",
                "needs_dispute": "主管核对中",
                "refund_reviewed": "待核验到账条件",
                "refunded": "已退入余额",
                "rejected": "未通过",
                "withdrawn": "已撤销",
                "open": "待处理",
                "assigned": "已分派",
                "in_progress": "处理中",
                "waiting_customer": "等待补充",
                "resolved": "已解决",
                "closed": "已关闭",
                "reopened": "已重开",
                "verifying": "核验中",
                "awaiting_intercept": "等待拦截",
                "intercept_failed": "拦截失败",
                "awaiting_liability": "等待责任核验",
            }
            if not result.get("items"):
                output.append("当前没有可访问的本人服务记录。")
            for row in result.get("items", []):
                output.append(
                    "本人服务记录："
                    + labels.get(row["status"], "状态待核对")
                    + "。申请、审核或填写单号不代表退款到账，可在服务记录查看详情。"
                )
        elif name == "experience.search":
            output.append(
                "已参考经审核的处置流程；执行仍须核对当前订单与适用政策。"
                if result.get("procedures")
                else "暂无适用的已审核处置流程，请补充情况或联系人工。"
            )
        elif name == "wallet.summary":
            account = result.get("account")
            output.append(
                "当前账户余额为" + money(account["balance_minor"]) + "。"
                if account
                else "未找到可用的本人余额账户。"
            )
        elif name in {"orders.list", "orders.detail"}:
            items = result.get("items", []) if name == "orders.list" else [result]
            if not items:
                output.append("当前查询范围内没有本人订单。")
            for order in items[:10]:
                output.append(
                    f"订单{order['display_no']}："
                    f"{TRADE.get(order['trade_status'], '状态待核对')}，"
                    f"{FULFILLMENT.get(order['fulfillment_status'], '履约状态待核对')}；"
                    f"原实付{money(order['paid_minor'])}，"
                    f"已退{money(order['refunded_minor'])}。"
                )
            if result.get("has_more") or len(items) > 10:
                output.append("还有其他订单，请到本人订单页面查看。")
        elif name == "shipments.list":
            if not result.get("items"):
                output.append("该订单暂没有发货记录。")
            for item in result.get("items", [])[:5]:
                signed = (
                    "承运方记录已签收" if item.get("carrier_signed_at") else "承运方尚无签收记录"
                )
                output.append(f"{item['carrier']}：{signed}。承运签收不等于您本人确认收货。")
        elif name == "knowledge.search":
            # 原文摘录保留来源，不生成超出证据的承诺。
            if not result.get("items"):
                output.append("目前没有有效的已发布资料支持这个回答，请补充商品或政策条件。")
            for index, item in enumerate(result.get("items", [])[:5], 1):
                output.append(f"[{index}] {item['text']}")
            if result.get("degraded"):
                output.append("部分检索服务暂不可用，以上仅依据当前可用资料。")
        elif name == "sql.query":
            if "rows" in result:
                import json

                output.append(
                    "本人受控查询结果：" + json.dumps(result["rows"][:10], ensure_ascii=False)
                )
                if len(result["rows"]) > 10:
                    output.append("结果较多，请缩小查询范围。")
            else:
                output.append("请补充查询的时间范围或业务条件。")
    answer = "\n".join(output)
    if len(answer) > 4000:
        # 不在退款条件/例外中间截断，再伪装成完整结论。
        return "本次资料较长，无法在单条回答完整展示。请缩小查询范围，并打开引用核对完整条款。"
    return answer
