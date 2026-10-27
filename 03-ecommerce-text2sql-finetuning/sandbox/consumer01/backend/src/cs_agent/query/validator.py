import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlglot import exp, parse
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from cs_agent.core.errors import AppError
from cs_agent.query.schema import DIGEST, PARAMETERS, REVISION, SCHEMA

ALLOWED = {
    "select",
    "column",
    "identifier",
    "table",
    "from",
    "join",
    "where",
    "and",
    "or",
    "not",
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "between",
    "is",
    "null",
    "boolean",
    "literal",
    "placeholder",
    "order",
    "ordered",
    "group",
    "having",
    "limit",
    "offset",
    "alias",
    "tablealias",
    "with",
    "cte",
    "subquery",
    "count",
    "sum",
    "min",
    "max",
    "avg",
    "coalesce",
    "datetrunc",
    "timestamptrunc",
    "paren",
    "add",
    "sub",
    "mul",
    "div",
    "neg",
    "distinct",
    "star",
    "var",
}
ENUMS = {
    "awaiting_payment",
    "paid",
    "completed",
    "cancelled_unpaid",
    "cancelled_refunded",
    "closed_refunded",
    "unshipped",
    "in_transit",
    "received_by_customer",
    "carrier_signed",
    "active",
    "frozen",
    "initialization",
    "topup",
    "payment",
    "refund",
    "CNY",
    "day",
    "week",
    "month",
}


def rejected(code="unsafe_query"):
    return AppError(code, 422, "查询未通过范围、结构或预算校验")


@dataclass(frozen=True)
class PreparedQuery:
    sql: str
    columns: list[str]
    parameters: set[str]
    parameter_fields: dict[str, set[str]] = field(default_factory=dict)


def validate_parameters(prepared, parameters, now):
    if not isinstance(parameters,dict) or set(parameters) != prepared.parameters:
        raise rejected("invalid_parameters")
    if {"start_at","end_at"}&prepared.parameters:
        if not {"start_at","end_at"}<=prepared.parameters or any(
            not isinstance(parameters[k],datetime) or parameters[k].tzinfo is None
            for k in ("start_at","end_at")
        ):
            raise rejected("invalid_range")
        date_range(now,parameters["start_at"],parameters["end_at"])
    for name in prepared.parameters-{"start_at","end_at"}:
        value = parameters[name]
        if not isinstance(value,str) or not 0<len(value)<=80 or value.strip()!=value or any(ord(c)<32 for c in value):
            raise rejected("invalid_parameters")
        if name=="status":
            allowed = {
                "trade_status":{"awaiting_payment","paid","completed","cancelled_unpaid","cancelled_refunded","closed_refunded"},
                "fulfillment_status":{"unshipped","in_transit","received_by_customer"},
                "status":{"active","frozen","in_transit","carrier_signed"},
                "kind":{"initialization","topup","payment","refund"},
            }
            if any(value not in allowed.get(column,set()) for column in prepared.parameter_fields.get(name,set())):
                raise rejected("invalid_parameters")


def date_range(now, start, end):
    end = end or now
    start = start or end - timedelta(days=30)
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or not 0 < (end - start).total_seconds() <= 366 * 86400
    ):
        raise rejected("invalid_range")
    return {"start_at": start, "end_at": end}


def validate_query(sql, *, schema_revision=REVISION, schema_digest=DIGEST):
    if schema_revision != REVISION or schema_digest != DIGEST or len(sql.encode()) > 8192:
        raise rejected("schema_mismatch")
    try:
        statements = parse(sql, read="postgres")
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise rejected()
        tree = statements[0]
        nodes = list(tree.walk())
        if len(nodes) > 512 or sum(isinstance(n, exp.Join) for n in nodes) > 3:
            raise rejected()
        if len(list(tree.find_all(exp.CTE))) > 2:
            raise rejected()
        for node in nodes:
            if node.key not in ALLOWED or node.depth > 18:
                raise rejected()
            if isinstance(node, exp.With) and node.args.get("recursive"):
                raise rejected()
            if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
                raise rejected()
            if isinstance(node, exp.Var) and (
                not isinstance(node.parent, (exp.DateTrunc, exp.TimestampTrunc))
                or node.name.lower() not in {"day", "week", "month"}
            ):
                raise rejected()
            if isinstance(node, exp.Table) and (
                node.db or node.catalog or not isinstance(node.this, exp.Identifier)
            ):
                raise rejected()
            if isinstance(node, exp.Literal):
                if node.is_string and node.this not in ENUMS:
                    raise rejected()
                if not node.is_string and (
                    not re.fullmatch(r"\d+(\.\d+)?", node.this) or float(node.this) > 1e9
                ):
                    raise rejected()
        parameters = {n.name for n in tree.find_all(exp.Placeholder)}
        if not parameters <= PARAMETERS:
            raise rejected()
        qualified = qualify(
            tree,
            dialect="postgres",
            schema=SCHEMA,
            validate_qualify_columns=True,
            expand_stars=False,
        )
        # 参数只能占据匹配字段的值位置，不可借order_id/status充当排序、投影、时区等。
        parameter_fields = {}
        expected = {"order_id":{"order_id"},"sku_id":{"sku_id"},
                    "status":{"trade_status","fulfillment_status","status","kind"},
                    "start_at":{"created_at","paid_at","received_at","shipped_at","carrier_signed_at","tracking_updated_at","occurred_at","as_of"}}
        expected["end_at"] = expected["start_at"]
        for placeholder in qualified.find_all(exp.Placeholder):
            parent = placeholder.parent
            column = None
            if isinstance(parent,(exp.EQ,exp.NEQ,exp.GT,exp.GTE,exp.LT,exp.LTE)):
                other = parent.expression if parent.this is placeholder else parent.this
                column = other if isinstance(other,exp.Column) else None
            elif isinstance(parent,(exp.In,exp.Between)) and parent.this is not placeholder:
                column = parent.this if isinstance(parent.this,exp.Column) else None
            if column is None or column.name not in expected[placeholder.name]:
                raise rejected("invalid_parameters")
            parameter_fields.setdefault(placeholder.name,set()).add(column.name)
        scopes = traverse_scope(qualified)
        for scope in scopes:
            sources = [source for _, source in scope.selected_sources.values()]
            for source in sources:
                if isinstance(source, exp.Table) and source.name not in SCHEMA:
                    raise rejected()
            # 先在单一正确粒度聚合，再连接。禁止连接订单明细后对订单金额重复求和。
            if len(sources) > 1 and any(
                n.find_ancestor(exp.Select) is scope.expression
                for n in scope.expression.find_all(exp.Sum, exp.Avg)
            ):
                raise rejected("ambiguous_grain")
            for join in scope.expression.args.get("joins", []):
                condition = join.args.get("on")
                if condition is None or not isinstance(condition, exp.EQ):
                    raise rejected()
                left, right = condition.this, condition.expression
                if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                    raise rejected()
                if left.name != right.name or left.name not in {
                    "order_id",
                    "sku_id",
                    "shipment_id",
                }:
                    raise rejected()
        columns = [p.alias_or_name for p in qualified.expressions]
        if (
            not columns
            or len(set(columns)) != len(columns)
            or any(
                not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", name)
                or re.search(r"phone|address|street|password|secret|token|owner|staff|cost_", name)
                for name in columns
            )
        ):
            raise rejected()
        limit = qualified.args.get("limit")
        size = (
            int(limit.expression.this)
            if limit and isinstance(limit.expression, exp.Literal)
            else 20
        )
        if size < 1:
            raise rejected()
        qualified = qualified.limit(min(size, 50))
        offset = qualified.args.get("offset")
        if offset and (
            not isinstance(offset.expression, exp.Literal) or int(offset.expression.this) > 10000
        ):
            raise rejected()
        rendered = qualified.sql(dialect="postgres")
        for parameter in parameters:
            rendered = rendered.replace(f"%({parameter})s", f":{parameter}")
        return PreparedQuery(rendered, columns, parameters, parameter_fields)
    except AppError:
        raise
    except Exception:
        raise rejected() from None
