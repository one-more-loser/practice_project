"""02确定性业务内核：只计算、校验，不访问数据库、模型或01执行接口。"""

import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from functools import wraps


class DomainError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


SCENARIOS = ("low", "base", "high")
ZERO = Decimal(0)
CENT = Decimal(1)
RATIO = Decimal("0.000001")


def _decimal_context(function):
    @wraps(function)
    def calculate(*args, **kwargs):
        # 聚合乘积可超28位，独立上下文避免默认精度截断，也不修改进程全局设置。
        with localcontext() as context:
            context.prec = 80
            return function(*args, **kwargs)
    return calculate


def _error(code, message):
    raise DomainError(code, message)


def _shape(value, required, optional=(), *, code, name):
    if not isinstance(value, dict):
        _error(code, f"{name}必须是对象")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        _error(code, f"{name}字段不符：缺少{sorted(missing)}，不支持{sorted(extra)}")


def _text(value, name, code, *, blank=False):
    if not isinstance(value, str) or (not blank and not value.strip()):
        _error(code, f"{name}必须是{'可为空的' if blank else '非空'}字符串")
    return value


def _integer(value, name, code, *, minimum=0):
    if (isinstance(value, bool) or not isinstance(value, int) or
            not minimum <= value <= 9223372036854775807):
        _error(code, f"{name}必须是≥{minimum}且在BIGINT范围内的整数")
    return value


def _money(value, name, code, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        _error(code, f"{name}必须是非负整数分字符串")
    # 限制为BIGINT范围，与后续持久化/跨服务传输保持一致。
    if len(value) > 19 or int(value) > 9223372036854775807:
        _error(code, f"{name}超出整数分范围")
    return Decimal(value)


def _decimal(value, name, code, *, positive=False, rate=False, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        _error(code, f"{name}必须是十进制字符串")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _error(code, f"{name}必须是有限十进制数")
    if not number.is_finite() or number < 0 or (positive and number == 0):
        _error(code, f"{name}必须是有限{'正数' if positive else '非负数'}")
    if rate and number > 1:
        _error(code, f"{name}须在0和1之间")
    if abs(number.adjusted()) > 20 or len(number.as_tuple().digits) > 24:
        _error(code, f"{name}精度或范围过大")
    return number


def _time(value, name, code, *, offset=None):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        _error(code, f"{name}不是ISO时间")
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        _error(code, f"{name}必须带时区")
    # UTC与上海时间可以表示同一瞬间；带时区后转为同一参考区比较。
    return parsed.astimezone(timezone(offset)) if offset is not None else parsed


def _list(value, name, code):
    if not isinstance(value, list):
        _error(code, f"{name}必须是数组")
    return value


def _unique(rows, key, code, name):
    result = {}
    for row in rows:
        if not isinstance(row, dict) or key not in row:
            _error(code, f"{name}缺少{key}")
        identity = _text(row[key], f"{name}.{key}", code)
        if identity in result and result[identity] != row:
            _error(code, f"{name}存在冲突重复主键：{identity}")
        result[identity] = row
    return list(result.values())


def _amount(value):
    if value is None:
        return None
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _hours(value):
    if value is None:
        return None
    text = format(value.quantize(RATIO, rounding=ROUND_HALF_UP), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _check(code, label, status, reason):
    return {"code": code, "label": label, "status": status, "reason": reason}


@_decimal_context
def validate_snapshot(snapshot: dict) -> dict:
    """整批校验并返回独立、按事件主键去重的快照；冲突批次全部拒收。"""
    code = "invalid_snapshot"
    _shape(snapshot, ("snapshot_id", "revision", "store_id", "source_kind", "as_of",
                      "currency", "skus", "orders", "refunds"),
           ("execution_supported_rules", "schema_version", "provenance", "source_metadata"), code=code, name="Snapshot")
    _text(snapshot["snapshot_id"], "snapshot_id", code)
    _text(snapshot["store_id"], "store_id", code)
    _integer(snapshot["revision"], "revision", code, minimum=1)
    if snapshot["source_kind"] not in ("synthetic", "business_import", "derived"):
        _error(code, "未知source_kind")
    if "source_metadata" in snapshot:
        metadata = snapshot["source_metadata"]
        _shape(metadata, ("producer", "source_revision", "cost_basis", "capacity_basis"), code=code, name="source_metadata")
        for key, value in metadata.items():
            _text(value, "source_metadata." + key, code, blank=key in ("cost_basis", "capacity_basis"))
    if snapshot.get("schema_version", "OPS-SNAPSHOT-v0.1") not in ("OPS-SNAPSHOT-v0.1", "OPS-SNAPSHOT-v0.2"):
        _error(code, "未知快照schema_version")
    if snapshot["source_kind"] == "derived" or "provenance" in snapshot:
        if snapshot.get("schema_version") != "OPS-SNAPSHOT-v0.2":
            _error(code, "derived父链须使用OPS-SNAPSHOT-v0.2")
        provenance = snapshot.get("provenance")
        _shape(provenance, ("parents", "transformation", "inputs_version"), code=code, name="provenance")
        for key in ("transformation", "inputs_version"):
            _text(provenance[key], key, code)
        parents = _list(provenance["parents"], "parents", code)
        if not parents or len(parents) > 20:
            _error(code, "派生快照须有1到20个可回放父引用")
        seen = set()
        for parent in parents:
            _shape(parent, ("snapshot_id", "revision", "digest"), code=code, name="parent")
            _text(parent["snapshot_id"], "parent.snapshot_id", code)
            _integer(parent["revision"], "parent.revision", code, minimum=1)
            if not isinstance(parent["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", parent["digest"]):
                _error(code, "父快照摘要必须为SHA256")
            identity = (parent["snapshot_id"], parent["revision"])
            if identity in seen or identity == (snapshot["snapshot_id"], snapshot["revision"]):
                _error(code, "父链重复或自引用")
            seen.add(identity)
    if snapshot["currency"] != "CNY":
        _error(code, "首版只接收CNY，禁止未知币种或混币汇总")
    cutoff = _time(snapshot["as_of"], "as_of", code)
    offset = cutoff.utcoffset()
    if "execution_supported_rules" in snapshot:
        for rule in _list(snapshot["execution_supported_rules"], "execution_supported_rules", code):
            _text(rule, "execution_supported_rules[]", code)

    clean = deepcopy(snapshot)
    for name, key in (("skus", "sku_id"), ("orders", "order_id"), ("refunds", "refund_id")):
        clean[name] = _unique(_list(clean[name], name, code), key, code, name)

    for sku in clean["skus"]:
        _shape(sku, ("sku_id", "title", "category", "base_price_minor", "cost_minor",
                     "variable_fee_minor", "available_qty", "price_version"),
               code=code, name="Sku")
        for name in ("title", "category"):
            _text(sku[name], f"sku.{name}", code)
        if _money(sku["base_price_minor"], "base_price_minor", code) <= 0:
            _error(code, "SKU原价必须大于0")
        for name in ("cost_minor", "variable_fee_minor"):
            _money(sku[name], name, code, nullable=True)
        if sku["available_qty"] is not None:
            _integer(sku["available_qty"], "available_qty", code)
        _integer(sku["price_version"], "price_version", code, minimum=1)

    sku_ids = {sku["sku_id"] for sku in clean["skus"]}
    order_by_id = {}
    for order in clean["orders"]:
        _shape(order, ("order_id", "paid_at", "status", "channel", "paid_minor", "items"),
               code=code, name="Order")
        if order["status"] not in ("paid", "unpaid", "cancelled"):
            _error(code, "未知订单状态")
        _text(order["channel"], "channel", code, blank=True)
        paid = _money(order["paid_minor"], "order.paid_minor", code)
        if order["paid_at"] is not None or order["status"] == "paid":
            _time(order["paid_at"], "paid_at", code, offset=offset)
        items = _list(order["items"], "items", code)
        if not items:
            _error(code, "订单须有订单行")
        allocated = ZERO
        for item in items:
            _shape(item, ("sku_id", "qty", "paid_minor"), code=code, name="OrderItem")
            _text(item["sku_id"], "item.sku_id", code)
            if item["sku_id"] not in sku_ids:
                _error(code, "订单行关联不存在的SKU")
            _integer(item["qty"], "item.qty", code, minimum=1)
            allocated += _money(item["paid_minor"], "item.paid_minor", code)
        if allocated != paid:
            _error(code, "订单金额与原生行分摊不平")
        order_by_id[order["order_id"]] = order

    refunded = defaultdict(lambda: ZERO)
    for refund in clean["refunds"]:
        _shape(refund, ("refund_id", "order_id", "occurred_at", "status", "amount_minor"),
               code=code, name="Refund")
        _text(refund["order_id"], "refund.order_id", code)
        if refund["order_id"] not in order_by_id:
            _error(code, "退款关联不存在的订单")
        if refund["status"] not in ("succeeded", "pending", "failed"):
            _error(code, "未知退款状态")
        occurred = _time(refund["occurred_at"], "occurred_at", code, offset=offset)
        amount = _money(refund["amount_minor"], "refund.amount_minor", code)
        original = order_by_id[refund["order_id"]]
        if refund["status"] == "succeeded":
            if original["status"] != "paid":
                _error(code, "未成功支付订单不能存在成功退款")
            if occurred < _time(original["paid_at"], "paid_at", code):
                _error(code, "退款时间不能早于原支付时间")
            refunded[refund["order_id"]] += amount
            if refunded[refund["order_id"]] > Decimal(original["paid_minor"]):
                _error(code, "成功退款累计超原实付")
    return clean


@_decimal_context
def calculate_metrics(snapshot: dict, start: datetime, end: datetime, as_of: datetime) -> dict:
    """现金期按事件窗，支付批次按原支付窗；两者共享可见时点但不混分母。"""
    source = validate_snapshot(snapshot)
    watermark = _time(source["as_of"], "snapshot.as_of", "invalid_window")
    offset = watermark.utcoffset()
    start = _time(start, "start", "invalid_window", offset=offset)
    end = _time(end, "end", "invalid_window", offset=offset)
    as_of = _time(as_of, "as_of", "invalid_window", offset=offset)
    if start >= end:
        _error("invalid_window", "时间窗须开始早于结束，采用[start,end)")
    visible = min(as_of, watermark)
    cohort = {}
    payment = ZERO
    units = 0
    channels = {}
    for order in source["orders"]:
        if order["status"] != "paid":
            continue
        paid_at = _time(order["paid_at"], "paid_at", "invalid_snapshot")
        if not (start <= paid_at < end and paid_at <= visible):
            continue
        cohort[order["order_id"]] = order
        amount = Decimal(order["paid_minor"])
        payment += amount
        units += sum(item["qty"] for item in order["items"])
        channel = order["channel"].strip() or "unknown"
        group = channels.setdefault(channel, {"amount": ZERO, "orders": 0})
        group["amount"] += amount
        group["orders"] += 1

    cash_refund = ZERO
    cohort_refund = ZERO
    refunded_orders = set()
    for refund in source["refunds"]:
        if refund["status"] != "succeeded":
            continue
        occurred = _time(refund["occurred_at"], "occurred_at", "invalid_snapshot")
        if occurred > visible:
            continue
        amount = Decimal(refund["amount_minor"])
        if start <= occurred < end:
            cash_refund += amount
        if refund["order_id"] in cohort:
            cohort_refund += amount
            refunded_orders.add(refund["order_id"])

    count = len(cohort)
    return {
        "payment_minor": _amount(payment), "refund_minor": _amount(cash_refund),
        "net_paid_minor": _amount(payment - cash_refund), "paid_order_count": count,
        "paid_units": units, "average_order_minor": _amount(payment / count) if count else None,
        "cohort_refund_order_rate": str((Decimal(len(refunded_orders)) / count).quantize(
            RATIO, rounding=ROUND_HALF_UP)) if count else None,
        "cohort_refund_amount_rate": str((cohort_refund / payment).quantize(
            RATIO, rounding=ROUND_HALF_UP)) if payment > 0 else None,
        "channels": [{"channel": name, "payment_minor": _amount(row["amount"]),
                      "paid_order_count": row["orders"]} for name, row in sorted(channels.items())],
    }


def _validate_draft(draft, skus, offset):
    code = "invalid_draft"
    _shape(draft, ("title", "snapshot_id", "expected_snapshot_revision", "start_at", "end_at",
                   "selected_candidate", "fixed_fee_minor", "goal", "lines", "resources",
                   "tasks", "monitoring"), ("notes",), code=code, name="Draft")
    _text(draft["title"], "title", code)
    _text(draft["snapshot_id"], "snapshot_id", code)
    _integer(draft["expected_snapshot_revision"], "expected_snapshot_revision", code, minimum=1)
    _time(draft["start_at"], "start_at", code, offset=offset)
    _time(draft["end_at"], "end_at", code, offset=offset)
    if draft["selected_candidate"] not in ("baseline", "direct", "coupon"):
        _error(code, "不支持的候选规则")
    _money(draft["fixed_fee_minor"], "fixed_fee_minor", code)
    if "notes" in draft:
        _text(draft["notes"], "notes", code, blank=True)
    goal = draft["goal"]
    _shape(goal, ("min_revenue_minor", "min_profit_minor", "budget_minor", "scenario_policy"),
           code=code, name="goal")
    for name in ("min_revenue_minor", "min_profit_minor", "budget_minor"):
        _money(goal[name], f"goal.{name}", code, nullable=True)
    if goal["scenario_policy"] not in (*SCENARIOS, "all"):
        _error(code, "须显式选择low/base/high/all情景政策")

    lines = _list(draft["lines"], "lines", code)
    if not lines:
        _error(code, "须选择至少一个SKU")
    selected_skus = set()
    for line in lines:
        _shape(line, ("sku_id", "quantities", "direct_price_minor", "basket_qty",
                      "coupon_minor", "coupon_threshold_minor"), code=code, name="line")
        sku_id = _text(line["sku_id"], "line.sku_id", code)
        if sku_id not in skus or sku_id in selected_skus:
            _error(code, "方案SKU不存在或重复；不支持跨SKU篮子")
        selected_skus.add(sku_id)
        _shape(line["quantities"], SCENARIOS, code=code, name="quantities")
        for name in SCENARIOS:
            _integer(line["quantities"][name], f"quantities.{name}", code)
        if not (line["quantities"]["low"] <= line["quantities"]["base"] <= line["quantities"]["high"]):
            _error(code, "销量情景须low≤base≤high")
        basket = _integer(line["basket_qty"], "basket_qty", code, minimum=1)
        if any(line["quantities"][name] % basket for name in SCENARIOS):
            _error(code, "情景销量须与单SKU每单篮子数量勾稽")
        direct = _money(line["direct_price_minor"], "direct_price_minor", code)
        base = Decimal(skus[sku_id]["base_price_minor"])
        if not 0 < direct <= base:
            _error(code, "活动单价须大于0且不高于原价")
        coupon = _money(line["coupon_minor"], "coupon_minor", code)
        _money(line["coupon_threshold_minor"], "coupon_threshold_minor", code)
        if coupon > base * basket:
            _error(code, "券不可产生负实付价格")

    resources = _list(draft["resources"], "resources", code)
    roles = set()
    for resource in resources:
        _shape(resource, ("role", "volume_per_unit", "productivity_per_hour", "people",
                          "shift_hours", "attendance", "efficiency", "source", "confirmed"),
               code=code, name="resource")
        role = _text(resource["role"], "resource.role", code)
        if role in roles:
            _error(code, "岗位重复；首版每岗位只接受一组容量参数")
        roles.add(role)
        _text(resource["source"], "resource.source", code, blank=True)
        if not isinstance(resource["confirmed"], bool):
            _error(code, "资源confirmed须为布尔值")
        if resource["people"] is not None:
            _integer(resource["people"], "people", code)
        for name in ("volume_per_unit", "productivity_per_hour", "shift_hours", "attendance", "efficiency"):
            _decimal(resource[name], name, code, nullable=True,
                     positive=name == "productivity_per_hour", rate=name in ("attendance", "efficiency"))

    for task in _list(draft["tasks"], "tasks", code):
        _shape(task, ("title", "owner", "due_at", "stage", "done"), code=code, name="task")
        _text(task["title"], "task.title", code, blank=True)
        _text(task["owner"], "task.owner", code, blank=True)
        if task["due_at"] is not None:
            _time(task["due_at"], "task.due_at", code, offset=offset)
        if task["stage"] not in ("before_publish", "during", "after"):
            _error(code, "不支持的任务阶段")
        if not isinstance(task["done"], bool):
            _error(code, "任务done须为布尔值")
    monitoring = draft["monitoring"]
    _shape(monitoring, ("interval_minutes", "data_source", "stop_conditions"),
           code=code, name="monitoring")
    if monitoring["interval_minutes"] is not None:
        _integer(monitoring["interval_minutes"], "interval_minutes", code, minimum=1)
    _text(monitoring["data_source"], "monitoring.data_source", code, blank=True)
    for condition in _list(monitoring["stop_conditions"], "stop_conditions", code):
        _text(condition, "stop_condition", code)


def _threshold(code, label, actual, bound, *, minimum):
    if actual is None or bound is None:
        return _check(code, label, "unknown", "缺少可靠数据或约束阈值")
    bound = Decimal(bound)
    passed = actual >= bound if minimum else actual <= bound
    difference = actual - bound if minimum else bound - actual
    return _check(code, label, "pass" if passed else "fail", f"约束余量{_amount(difference)}分")


def _calculate(candidate_id, scenario, draft, skus):
    revenue, cost, fees, base_total = ZERO, ZERO, ZERO, ZERO
    units = 0
    checks = []
    for line in draft["lines"]:
        sku = skus[line["sku_id"]]
        qty = line["quantities"][scenario]
        base = Decimal(sku["base_price_minor"])
        line_revenue = base * qty
        if candidate_id == "direct":
            price = Decimal(line["direct_price_minor"])
            line_revenue = price * qty
            checks.append(_check(f"direct_price:{line['sku_id']}", "独立直降活动单价",
                                 "pass" if price < base else "fail", "活动单价须低于原价"))
        elif candidate_id == "coupon":
            eligible = base * line["basket_qty"] >= Decimal(line["coupon_threshold_minor"])
            if eligible:
                line_revenue -= Decimal(line["coupon_minor"]) * (qty // line["basket_qty"])
            checks.append(_check(f"coupon_eligibility:{line['sku_id']}", "单SKU篮子满额条件",
                                 "pass" if eligible else "fail", "按原价单SKU篮子、含门槛、每单最多一券"))
        revenue += line_revenue
        base_total += base * qty
        units += qty
        cost = None if cost is None or sku["cost_minor"] is None else cost + Decimal(sku["cost_minor"]) * qty
        fees = None if fees is None or sku["variable_fee_minor"] is None else fees + Decimal(sku["variable_fee_minor"]) * qty
        inventory = sku["available_qty"]
        checks.append(_check(f"inventory:{line['sku_id']}", f"{line['sku_id']}现货库存",
                             "unknown" if inventory is None else "pass" if qty <= inventory else "fail",
                             "缺少有效可用库存" if inventory is None else f"需求{qty}件，可用{inventory}件"))
    fixed = Decimal(draft["fixed_fee_minor"])
    gross = revenue - cost if cost is not None else None
    profit = gross - fees - fixed if gross is not None and fees is not None else None
    budget = base_total - revenue + fixed
    goal = draft["goal"]
    checks.extend((
        _threshold("revenue", "商品实收底线", revenue, goal["min_revenue_minor"], minimum=True),
        _threshold("profit", "贡献利润底线", profit, goal["min_profit_minor"], minimum=True),
        _threshold("budget", "商家让利预算", budget, goal["budget_minor"], minimum=False),
    ))
    return {"revenue_minor": _amount(revenue), "gross_profit_minor": _amount(gross),
            "contribution_profit_minor": _amount(profit), "budget_used_minor": _amount(budget),
            "units": units, "checks": checks}


def _resources(draft, units, duration_hours):
    rows, checks = [], []
    by_role = {row["role"]: row for row in draft["resources"]}
    for role in sorted(set(by_role) | {"客服", "仓储"}):
        resource = by_role.get(role)
        result = {"role": role, "required_hours": None, "available_hours": None,
                  "gap_hours": None, "additional_people": None,
                  "source": resource["source"] if resource else "",
                  "confirmed": resource["confirmed"] if resource else False}
        parameters = ("volume_per_unit", "productivity_per_hour", "people",
                      "shift_hours", "attendance", "efficiency")
        shift = Decimal(resource["shift_hours"]) if resource and resource["shift_hours"] is not None else None
        if shift is not None and shift > duration_hours:
            checks.append(_check(f"resource_shift:{role}", f"{role}排班时间", "fail",
                                 "单人排班工时超过活动窗；V1未声明活动窗外履约安排"))
        if resource is None or any(resource[name] is None for name in parameters):
            checks.append(_check(f"resource:{role}", f"{role}容量", "unknown", "缺少岗位或容量参数"))
        else:
            required = units * Decimal(resource["volume_per_unit"]) / Decimal(resource["productivity_per_hour"])
            # 超窗排班仍返回风险明细，但不能把窗外时间计入本活动的有效容量。
            per_person = min(shift, duration_hours) * Decimal(resource["attendance"]) * Decimal(resource["efficiency"])
            available = resource["people"] * per_person
            gap = max(ZERO, required - available)
            additional = int((gap / per_person).to_integral_value(rounding=ROUND_CEILING)) if per_person else None
            result.update(required_hours=_hours(required), available_hours=_hours(available),
                          gap_hours=_hours(gap), additional_people=additional if gap else 0)
            if gap:
                status, reason = "fail", "有效工时不足；不自动缩减销量"
            elif not resource["source"].strip() or not resource["confirmed"]:
                status, reason = "unknown", "容量依据或岗位确认未齐全"
            else:
                status, reason = "pass", "所需工时不高于已确认有效容量"
            checks.append(_check(f"resource:{role}", f"{role}容量", status, reason))
        rows.append(result)
    return rows, checks


def _tasks(draft, start, end):
    checks = []
    tasks = draft["tasks"]
    for stage in ("before_publish", "during", "after"):
        found = any(task["stage"] == stage for task in tasks)
        checks.append(_check(f"task_stage:{stage}", f"{stage}责任安排",
                             "pass" if found else "unknown", "须有发布前、活动中和结束后复盘任务"))
    for index, task in enumerate(tasks):
        identity = f"task:{index}"
        if not task["title"].strip() or not task["owner"].strip() or task["due_at"] is None:
            checks.append(_check(identity, "任务完整度", "unknown", "缺任务标题、负责人或截止时间"))
            continue
        due = _time(task["due_at"], "task.due_at", "invalid_draft")
        stage = task["stage"]
        in_window = (due <= start if stage == "before_publish" else
                     start <= due < end if stage == "during" else due >= end)
        completed = task["done"] if stage == "before_publish" else True
        checks.append(_check(identity, task["title"], "pass" if in_window and completed else "fail",
                             "发布前任务须完成；各阶段截止时间须与活动窗一致"))
    monitor = draft["monitoring"]
    complete = bool(monitor["interval_minutes"] and monitor["data_source"].strip() and monitor["stop_conditions"])
    checks.append(_check("monitoring", "监控与停止条件", "pass" if complete else "unknown",
                         "须有检查频率、数据源和停止阈值；来源声明不表示真实01已接通"))
    return checks


@_decimal_context
def evaluate_plan(draft: dict, snapshot: dict, now: datetime,
                  supported_rules: list[str] | None = None) -> dict:
    """返回02检查结果；checks_passed不授予真实01预检、审批或发布权限。"""
    source = validate_snapshot(snapshot)
    as_of = _time(source["as_of"], "as_of", "invalid_snapshot")
    now = _time(now, "now", "invalid_window", offset=as_of.utcoffset())
    skus = {sku["sku_id"]: sku for sku in source["skus"]}
    _validate_draft(draft, skus, as_of.utcoffset())
    if (draft["snapshot_id"] != source["snapshot_id"] or
            draft["expected_snapshot_revision"] != source["revision"]):
        _error("snapshot_revision_conflict", "方案引用的快照或修订与当前快照不一致，须重算")
    start = _time(draft["start_at"], "start_at", "invalid_draft")
    end = _time(draft["end_at"], "end_at", "invalid_draft")
    if start >= end:
        _error("invalid_draft", "活动时间须开始早于结束")
    if supported_rules is not None:
        for rule in _list(supported_rules, "supported_rules", "invalid_draft"):
            _text(rule, "supported_rules[]", "invalid_draft")
    # 只接受调用方显式能力；快照自报的执行字段不参与授权。
    supported = {"direct"} if supported_rules is None else set(supported_rules)
    policy = draft["goal"]["scenario_policy"]
    checked_scenarios = SCENARIOS if policy == "all" else (policy,)
    duration = end - start
    duration_hours = (Decimal(duration.days * 86400 + duration.seconds)
                      + Decimal(duration.microseconds) / Decimal(1000000)) / Decimal(3600)
    candidates = []
    for identity, label in (("baseline", "维持原价"), ("direct", "独立直降"), ("coupon", "单SKU满额券")):
        scenarios = {name: _calculate(identity, name, draft, skus) for name in SCENARIOS}
        for scenario in scenarios.values():
            scenario["resources"], scenario["resource_checks"] = _resources(
                draft, scenario["units"], duration_hours,
            )
        candidates.append({
            "id": identity, "label": label, "scenarios": scenarios,
            "calculation_passed": all(check["status"] == "pass" for name in checked_scenarios
                                      for check in scenarios[name]["checks"]),
            "rule_supported": identity != "baseline" and identity in supported,
        })
    ranking_scenario = "base" if policy == "all" else policy
    candidates.sort(key=lambda row: (
        not row["calculation_passed"],
        row["scenarios"][ranking_scenario]["contribution_profit_minor"] is None,
        -Decimal(row["scenarios"][ranking_scenario]["contribution_profit_minor"] or "0"),
        Decimal(row["scenarios"][ranking_scenario]["budget_used_minor"]), row["id"],
    ))
    selected = next(row for row in candidates if row["id"] == draft["selected_candidate"])
    units = max(selected["scenarios"][name]["units"] for name in checked_scenarios)
    resources, resource_checks = _resources(draft, units, duration_hours)
    task_checks = _tasks(draft, start, end)
    age = now - as_of
    freshness = _check("snapshot_freshness", "快照与库存时效",
                       "pass" if timedelta(0) <= age <= timedelta(hours=24) else "unknown",
                       "带时区时间按等价瞬间比较，快照不可来自未来且距参考时钟≤24小时")
    window = _check("activity_window", "当前活动起止时间", "pass" if start >= now else "fail",
                    "当前确认不可使用已经开始的活动窗")
    execution = _check("rule_support", "02规则支持范围",
                       "pass" if selected["rule_supported"] else "unknown",
                       "baseline仅比较；券需显式执行支持；真实01另行预检与审批")
    global_checks = [freshness, window, execution, *resource_checks]
    all_checks = [*global_checks, *task_checks,
                  *(check for name in checked_scenarios for check in selected["scenarios"][name]["checks"])]
    statuses = {check["status"] for check in all_checks}
    feasibility = "infeasible" if "fail" in statuses else "needs_data" if "unknown" in statuses else "checks_passed"
    issues = [f"{check['label']}：{check['reason']}" for check in all_checks if check["status"] != "pass"]
    return {
        "candidates": candidates, "selected_candidate": draft["selected_candidate"],
        "feasibility": feasibility, "issues": list(dict.fromkeys(issues)), "resources": resources,
        "task_checks": task_checks,
        "lineage": {
            "snapshot_id": source["snapshot_id"], "snapshot_revision": source["revision"],
            "store_id": source["store_id"], "source_kind": source["source_kind"],
            "as_of": source["as_of"], "evaluated_at": now.isoformat(), "currency": source["currency"],
            "metric_revision": "OPS-METRIC-v0.1", "calculation_revision": "OPS-CALC-v0.1",
            "scenario_policy": policy, "checks": global_checks,
            "validation_scope": "仅02经营、库存、岗位、任务与声明的规则检查；不代表01预检通过或真实可执行",
            "coupon_assumption": "每SKU独立篮子；所有符合门槛的情景订单每单使用一券，不跨SKU合并",
            "ratio_precision": 6, "rounding": "ROUND_HALF_UP",
        },
    }
