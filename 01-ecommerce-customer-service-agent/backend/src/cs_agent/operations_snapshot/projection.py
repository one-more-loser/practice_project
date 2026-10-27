import hashlib
import json
from datetime import UTC, datetime

from cs_agent.core.errors import AppError


def source_gap():
    return AppError("snapshot_source_gap", 503, "经营源存在未满足的数据契约")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def snapshot_identity(store_id, batch_id, generation):
    return "ops_snapshot_" + digest([store_id, batch_id, generation])[:48]


def time_text(value):
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise source_gap()
    return instant.astimezone(UTC).isoformat()


def read_inputs(path, store_id, batch_id, generation):
    if path is None:
        return None
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
        if (set(value) != {"schema_version", "source_kind", "inputs_version", "store_id",
                          "data_batch_id", "batch_generation", "skus", "capacity_basis"}
            or value["schema_version"] != "OPS-SYNTHETIC-INPUTS-v1"
            or value["source_kind"] != "synthetic"
            or not isinstance(value["inputs_version"], str) or not value["inputs_version"]
            or value["store_id"] != store_id or value["data_batch_id"] != batch_id
            or value["batch_generation"] != generation
            or not isinstance(value["capacity_basis"], str) or not value["capacity_basis"]
            or not isinstance(value["skus"], dict)):
            raise ValueError("invalid synthetic input scope")
        for sku_id, row in value["skus"].items():
            if (not isinstance(sku_id, str) or not sku_id
                or set(row) != {"cost_minor", "variable_fee_minor"}
                or any(type(amount) is not int or amount < 0 for amount in row.values())):
                raise ValueError("invalid synthetic sku input")
    except (OSError, ValueError, TypeError, AttributeError):
        raise source_gap() from None
    value["sha256"] = hashlib.sha256(raw).hexdigest()
    return value


def build_projection(scope, rows, now, inputs=None):
    """只投影原生支付/退款，不从现金退款推断库存或利润。"""
    if scope["source_kind"] not in {"synthetic", "business_import"}:
        raise source_gap()
    if inputs is not None and scope["source_kind"] != "synthetic":
        raise source_gap()
    as_of = time_text(now)
    groups = {name: [] for name in ("skus", "orders", "items", "refunds")}
    for row in rows:
        groups[row["kind"]].append(row["body"])
    skus = []
    known = set()
    for sku in sorted(groups["skus"], key=lambda r: r["sku_id"]):
        known.add(sku["sku_id"])
        supplied = (inputs or {}).get("skus", {}).get(sku["sku_id"])
        skus.append({
            "sku_id": sku["sku_id"], "title": sku["title"], "category": sku["category"],
            "base_price_minor": str(sku["base_price_minor"]),
            "cost_minor": str(supplied["cost_minor"]) if supplied else (
                None if sku["cost_minor"] is None else str(sku["cost_minor"])),
            "variable_fee_minor": str(supplied["variable_fee_minor"]) if supplied else None,
            "available_qty": sku["available_qty"], "price_version": sku["price_version"],
        })
    if inputs and not set(inputs["skus"]).issubset(known):
        raise source_gap()
    items = {}
    for item in sorted(groups["items"], key=lambda r: r["order_item_id"]):
        if item["sku_id"] not in known:
            raise source_gap()
        items.setdefault(item["order_id"], []).append({
            "sku_id": item["sku_id"], "qty": item["qty"], "paid_minor": str(item["paid_minor"]),
        })
    if not set(items).issubset({order["order_id"] for order in groups["orders"]}):
        raise source_gap()
    orders = []
    paid_by_id = {}
    paid_times = {}
    for order in sorted(groups["orders"], key=lambda r: r["order_id"]):
        lines = items.get(order["order_id"], [])
        if not lines or sum(int(line["paid_minor"]) for line in lines) != order["paid_minor"]:
            # 01运费独立于原生商品行，02首发无运费字段；拒绝编造SKU行分摊。
            raise source_gap()
        if order["paid_minor"] > 0 and order["paid_at"] is None:
            raise source_gap()
        paid_at = None if order["paid_at"] is None else time_text(order["paid_at"])
        if paid_at is not None and datetime.fromisoformat(paid_at) > now:
            continue
        status = "paid" if order["paid_minor"] > 0 else (
            "cancelled" if order["trade_status"] == "cancelled_unpaid" else "unpaid")
        orders.append({"order_id": order["order_id"], "paid_at": paid_at, "status": status,
                       "channel": "", "paid_minor": str(order["paid_minor"]), "items": lines})
        paid_by_id[order["order_id"]] = order["paid_minor"]
        paid_times[order["order_id"]] = paid_at
    refunds, totals = [], {}
    for refund in sorted(groups["refunds"], key=lambda r: r["refund_id"]):
        occurred_at = time_text(refund["occurred_at"])
        if datetime.fromisoformat(occurred_at) > now:
            continue
        identity = refund["order_id"]
        if (identity not in paid_by_id or paid_times[identity] is None
            or datetime.fromisoformat(occurred_at) < datetime.fromisoformat(paid_times[identity])):
            raise source_gap()
        totals[identity] = totals.get(identity, 0) + refund["amount_minor"]
        if totals[identity] > paid_by_id[identity]:
            raise source_gap()
        refunds.append({"refund_id": refund["refund_id"], "order_id": identity,
                        "occurred_at": occurred_at, "status": "succeeded",
                        "amount_minor": str(refund["amount_minor"])})
    source_revision = digest({"scope": scope, "skus": skus, "orders": orders, "refunds": refunds,
                              "inputs_sha": inputs["sha256"] if inputs else None})
    cost_basis = "sku.cost_minor; variable_fee unknown"
    capacity_basis = "unknown: no approved staffing/capacity source"
    if inputs:
        cost_basis += "; synthetic inputs=" + inputs["inputs_version"] + ";sha256=" + inputs["sha256"]
        capacity_basis = "synthetic: " + inputs["capacity_basis"] + ";inputs=" + inputs["inputs_version"]
    return {
        "snapshot_id": snapshot_identity(scope["store_id"], scope["data_batch_id"],
                                         scope["batch_generation"]),
        "revision": 1, "store_id": scope["store_id"], "source_kind": scope["source_kind"],
        "schema_version": "OPS-SNAPSHOT-v0.1", "as_of": as_of, "currency": "CNY",
        "skus": skus, "orders": orders, "refunds": refunds,
        "source_metadata": {"producer": "cs01:operations-snapshot",
                            "source_revision": source_revision, "cost_basis": cost_basis,
                            "capacity_basis": capacity_basis},
    }
