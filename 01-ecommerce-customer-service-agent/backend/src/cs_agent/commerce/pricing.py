"""商品原价、活动让利、实付分开保存；退款沿原分摊算法。"""

from cs_agent.core.errors import AppError

MAX_MINOR = 2**63 - 1


def line_amounts(base, effective, qty):
    if (any(type(value) is not int for value in (base, effective, qty))
            or not 0 < effective <= base <= MAX_MINOR or not 0 < qty <= 2147483647
            or base * qty > MAX_MINOR):
        raise AppError("invalid_input", 422, "价格或数量超出系统可表示范围")
    return {"goods_minor": str(base * qty), "discount_minor": str((base - effective) * qty),
            "allocated_minor": str(effective * qty)}


def quote_amounts(lines):
    totals = tuple(sum(int(line[key]) for line in lines)
                   for key in ("goods_minor", "discount_minor", "allocated_minor"))
    if any(value > MAX_MINOR for value in totals) or totals[2] != totals[0] - totals[1]:
        raise AppError("invalid_input", 422, "订单金额超出系统可表示范围")
    return totals
