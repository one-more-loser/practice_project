"""不依赖数据库的金额、时限和输入规则。"""

import re
from datetime import timedelta
from itertools import islice

from cs_agent.core.errors import AppError
from cs_agent.privacy.redaction import external_text


def conflict(message):
    raise AppError("state_conflict", 409, message)


def evidence_refs(values):
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= 12
        or any(
            not isinstance(value, str)
            or not re.fullmatch(
                r"(?:evidence|shipment|inspection|policy|ticket):[A-Za-z0-9_.:-]{1,120}", value
            )
            for value in values
        )
    ):
        raise AppError("invalid_input", 422, "请提供本项目受控证据引用，不接受外部网址")
    return list(dict.fromkeys(values))


def clean_text(value, maximum=1000):
    if not isinstance(value, str) or not value.strip():
        raise AppError("invalid_input", 422, "请填写处理依据或申请说明")
    return external_text(value.strip(), max_length=maximum)


def policy_for(order):
    policy = order.get("policy_snapshot")
    if not isinstance(policy, dict) or not policy.get("aftersale_revision"):
        conflict("缺少订单适用政策，请先由人工核对，不能套用当前政策")
    return policy


def within_window(received_at, now, policy):
    if received_at is None or policy.get("ordinary_return_hours") != 168:
        conflict("收货时间或适用时限缺失，请人工核对")
    if now < received_at:
        conflict("收货时间不符合事实，请人工核对")
    return now <= received_at + timedelta(hours=168)


def original_freight(order, reason, requested):
    policy = policy_for(order)
    if not requested:
        return 0
    if reason == "ordinary":
        conflict("普通退货不退原订单运费")
    if order["freight_minor"] == 0:
        return 0
    # 金额只取历史订单政策；工作人员不能在接口里指定金额。
    value = policy.get(f"{reason}_original_freight_refund_minor")
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,18}", value):
        conflict("原订单运费缺少适用政策依据，请人工核对")
    amount = int(value)
    if amount > order["freight_minor"]:
        conflict("政策运费超出原订单运费，请人工核对")
    return amount


def allocation_parts(line):
    raw, quantity = line["unit_allocations"], line["ordered_qty"]
    try:
        if line["allocation_revision"] != "ALLOC-LAST-UNIT-v1" or raw["quantity"] != quantity:
            raise ValueError
        base, tail = int(raw["base_minor"]), int(raw["tail_minor"])
        if quantity <= 0 or base < 0 or tail < base or base * (quantity - 1) + tail != line["paid_goods_minor"]:
            raise ValueError
        return quantity, base, tail
    except (KeyError, TypeError, ValueError):
        conflict("原实付单位分摊不完整，请人工核对")


def allocate_units(line, quantity, occupied):
    if type(quantity) is not int or quantity <= 0:
        raise AppError("invalid_input", 422, "退货数量须为正整数")
    ordered, base, tail = allocation_parts(line)
    if (
        quantity + line["reserved_refund_qty"] + line["refunded_qty"] > ordered
    ):
        conflict("退货数量已被其他有效申请占用或已退款")
    # 原分摊为等额单位＋最后一件尾差；只找本次申请需要的索引，不展开整单。
    indices = list(islice((i for i in range(ordered) if i not in occupied), quantity))
    if len(indices) != quantity:
        conflict("退货数量已被其他有效申请占用或已退款")
    total = sum(tail if index == ordered - 1 else base for index in indices)
    if (
        total + line["reserved_refund_minor"] + line["refunded_goods_minor"]
        > line["paid_goods_minor"]
    ):
        conflict("可退商品金额不足，请核对原申请")
    return indices, total


def inspection_lines(items, provided):
    expected = {line["order_item_id"]: line["qty"] for line in items}
    if not isinstance(provided, dict) or set(provided) != set(expected):
        raise AppError("invalid_input", 422, "请核验全部申请明细")
    matched = True
    for ref, values in provided.items():
        if not isinstance(values, dict) or set(values) != {"received_qty", "saleable_qty"}:
            raise AppError("invalid_input", 422, "请分别填写实收数量与可售数量")
        received, saleable = values["received_qty"], values["saleable_qty"]
        if (
            type(received) is not int
            or type(saleable) is not int
            or not 0 <= saleable <= received <= expected[ref]
        ):
            raise AppError("invalid_input", 422, "可售量不能超过实收量，实收量不能超过申请量")
        matched = matched and received == expected[ref]
    return matched
