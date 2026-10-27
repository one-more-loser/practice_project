"""03-local SQL candidate validator.

It mirrors the 01 B2 rules that are relevant to the 6-8 question sandbox:
one read-only SELECT over the six approved views, 5 parameter names,
bounded literals, no write/DDL, no star except count(*), <=3 joins, <=2 CTEs,
<=8192 bytes, <=512 nodes, depth <=18, LIMIT <=50. It is deliberately
stricter than "SQL runs"; it is not 01's production validator and is not a
security boundary by itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

VIEW_COLUMNS = {
    "cs_order_self": {
        "order_id", "created_at", "paid_at", "received_at", "trade_status",
        "fulfillment_status", "goods_minor", "discount_minor", "freight_minor",
        "payable_minor", "paid_minor", "refunded_minor", "net_paid_minor", "currency",
    },
    "cs_order_item_self": {
        "order_item_id", "order_id", "sku_id", "product_name", "ordered_qty",
        "paid_goods_minor", "refunded_qty", "refunded_goods_minor",
        "remaining_qty", "remaining_goods_minor",
    },
    "cs_shipment_self": {
        "shipment_id", "order_id", "carrier", "status", "shipped_at",
        "carrier_signed_at", "tracking_updated_at",
    },
    "cs_wallet_summary_self": {"account_id", "balance_minor", "currency", "status"},
    "cs_wallet_entry_self": {
        "entry_id", "kind", "amount_minor", "sequence", "balance_before_minor",
        "balance_after_minor", "occurred_at", "currency",
    },
    "cs_product_public": {
        "product_id", "sku_id", "name", "category", "currency", "price_minor",
        "available_qty", "price_version", "as_of",
    },
}
ALLOWED_PARAMETERS = {"start_at", "end_at", "order_id", "sku_id", "status"}
ALLOWED_FUNCTIONS = {"count", "sum", "min", "max", "avg", "coalesce", "date_trunc", "timestamp_trunc", "time_trunc"}
ALLOWED_TYPES = (exp.Select,)
STRING_ENUMS = {
    "awaiting_payment", "paid", "completed", "cancelled_unpaid", "cancelled_refunded",
    "closed_refunded", "unshipped", "in_transit", "received_by_customer",
    "carrier_signed", "active", "frozen", "initialization", "topup", "payment", "refund",
    "day", "week", "month", "CNY",
}
MAX_BYTES = 8192
MAX_NODES = 512
MAX_DEPTH = 18
MAX_JOINS = 3
MAX_CTES = 2
MAX_LIMIT = 50
MAX_OFFSET = 10000


@dataclass
class Validation:
    ok: bool
    code: str = ""
    message: str = ""
    parameter_names: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    staged_sql: str = ""
    ast_sha256: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "code": self.code, "message": self.message,
            "parameter_names": self.parameter_names, "tables": self.tables,
            "ast_sha256": self.ast_sha256,
        }


def _fail(code: str, message: str) -> Validation:
    return Validation(False, code, message)


def _safe_outer_count_over_distinct(tree, aggs) -> bool:
    if tree.args.get("joins") or len(tree.expressions) != 1 or len(aggs) != 1:
        return False
    outer = tree.expressions[0]
    if isinstance(outer, exp.Alias):
        outer = outer.this
    if outer is not aggs[0] or not isinstance(outer, exp.Count) or not isinstance(outer.this, exp.Star):
        return False
    from_ = tree.args.get("from")
    if from_ is None or not isinstance(from_.this, exp.Subquery):
        return False
    inner = from_.this.this
    if not isinstance(inner, exp.Select) or len(inner.expressions) != 1:
        return False
    distinct = inner.args.get("distinct")
    if distinct is None or distinct.args.get("on") is not None:
        return False
    column = inner.expressions[0]
    return isinstance(column, exp.Column) and column.name.lower() == "order_id"


def validate(sql: str, parameter_names: list[str] | None = None) -> Validation:
    raw = sql.encode("utf-8")
    if len(raw) > MAX_BYTES:
        return _fail("sql_too_large", f"SQL is {len(raw)} bytes > {MAX_BYTES}")
    try:
        parsed = sqlglot.parse(sql, read="postgres")
    except Exception as exc:  # noqa: BLE001
        return _fail("parse_error", f"{type(exc).__name__}: {exc}")
    if len(parsed) != 1 or parsed[0] is None:
        return _fail("not_single_statement", "exactly one statement is required")
    tree = parsed[0]
    if not isinstance(tree, ALLOWED_TYPES):
        return _fail("unsafe_query", f"root {type(tree).__name__} is not a SELECT")
    if tree.find(exp.Union):
        return _fail("unsafe_query", "UNION is not allowed")
    if re.search(r"\b[EX]'|\$[A-Za-z_]*\$", sql):
        return _fail("unsafe_query", "E/X/dollar-quoted literals are not allowed")
    if tree.find(exp.Mod):
        return _fail("unsafe_query", "modulo operator is not allowed")
    for bad in (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop,
                exp.Alter, exp.Command, exp.Copy, exp.Grant, exp.Transaction):
        if tree.find(bad):
            return _fail("unsafe_query", f"{bad.__name__} is not allowed")
    for bad, label in ((exp.Case, "CASE"), (exp.Cast, "CAST"), (exp.Like, "LIKE"),
                       (exp.Window, "window")):
        if tree.find(bad):
            return _fail("unsafe_query", f"{label} is not in the approved expression set")

    nodes = list(tree.walk())
    if len(nodes) > MAX_NODES:
        return _fail("sql_too_complex", f"{len(nodes)} AST nodes > {MAX_NODES}")
    if _depth(tree) > MAX_DEPTH:
        return _fail("sql_too_complex", f"AST depth > {MAX_DEPTH}")

    ctes = list(tree.find_all(exp.CTE))
    if len(ctes) > MAX_CTES:
        return _fail("sql_too_complex", f"{len(ctes)} CTEs > {MAX_CTES}")
    cte_names = {cte.alias_or_name.lower() for cte in ctes}

    tables = list(tree.find_all(exp.Table))
    view_names = []
    aliases: dict[str, str] = {}
    for table in tables:
        name = table.name.lower()
        if table.db or table.catalog:
            return _fail("unapproved_resource", f"schema/catalog qualifier on {name}")
        if name in cte_names:
            continue
        if name not in VIEW_COLUMNS:
            return _fail("unapproved_resource", f"table {name!r} is not one of the six views")
        view_names.append(name)
        aliases[table.alias_or_name.lower()] = name

    join_count = len(list(tree.find_all(exp.Join)))
    if join_count > MAX_JOINS:
        return _fail("sql_too_complex", f"{join_count} joins > {MAX_JOINS}")

    for star in tree.find_all(exp.Star):
        parent = star.parent
        if not (isinstance(parent, exp.Count) and isinstance(parent.this, exp.Star)):
            return _fail("unsafe_query", "SELECT * is not allowed except count(*)")

    for join in tree.find_all(exp.Join):
        if join.args.get("using"):
            return _fail("unsafe_query", "JOIN USING is not allowed")
        on = join.args.get("on")
        if on is None:
            return _fail("unsafe_query", "JOIN without ON is not allowed")
        if not isinstance(on, exp.EQ):
            return _fail("unsafe_query", "JOIN ON must be a single equality")
        if not isinstance(on.this, exp.Column) or not isinstance(on.expression, exp.Column):
            return _fail("unsafe_query", "JOIN ON must compare two columns")
        if on.this.name.lower() not in {"order_id", "sku_id", "shipment_id"}:
            return _fail("unsafe_query", f"JOIN key {on.this.name!r} is not allowed")
        if on.this.name.lower() != on.expression.name.lower():
            return _fail("unsafe_query", "JOIN must compare the same key name")
        if on.this.table.lower() == on.expression.table.lower():
            return _fail("unsafe_query", "JOIN self-comparison is not allowed")

    for col in tree.find_all(exp.Column):
        name = col.name.lower()
        table = col.table.lower() if col.table else ""
        if table:
            if table not in aliases:
                return _fail("unknown_table", f"alias {table!r} is not a view in this query")
            if name not in VIEW_COLUMNS[aliases[table]]:
                return _fail("unknown_column", f"{table}.{name} is not a visible column")
        else:
            if len(view_names) == 1:
                if name not in VIEW_COLUMNS[view_names[0]]:
                    return _fail("unknown_column", f"{name} is not visible in {view_names[0]}")
            elif not any(name in cols for cols in VIEW_COLUMNS.values()):
                return _fail("unknown_column", f"{name} is not a visible column")
        if any(part in name for part in (
            "phone", "address", "street", "password", "secret", "token",
            "owner", "staff", "cost_",
        )):
            return _fail("unsafe_column", f"{name} is a forbidden column fragment")

    for func in tree.find_all(exp.Func):
        if isinstance(func, (exp.Connector, exp.Predicate, exp.Binary)):
            continue
        fname = func.name.lower() if isinstance(func, exp.Anonymous) else func.sql_name().lower()
        if fname not in ALLOWED_FUNCTIONS:
            return _fail("unapproved_function", f"function {fname!r} is not allowed")

    aggs = list(tree.find_all(exp.AggFunc))
    if aggs and len(view_names) > 1 and not _safe_outer_count_over_distinct(tree, aggs):
        # Conservative grain guard: a multi-view aggregate must group by the
        # join key(s). A single local exception is an outer COUNT(*) over a
        # SELECT DISTINCT order_id derived table: the inner DISTINCT has
        # already fixed the order grain. Flat join+SUM, dead CTE and IN
        # subquery forms remain rejected.
        group = tree.args.get("group")
        if group is None:
            return _fail("ambiguous_grain", "aggregate over a multi-view join requires GROUP BY")
        grouped = {c.name.lower() for g in group.expressions for c in g.find_all(exp.Column)}
        join_keys = {j.args["on"].this.name.lower() for j in tree.find_all(exp.Join)
                     if isinstance(j.args.get("on"), exp.EQ)}
        if not join_keys or not join_keys.issubset(grouped):
            return _fail("ambiguous_grain", "multi-view aggregate must group by the join key")

    for lit in tree.find_all(exp.Literal):
        if lit.is_string:
            if lit.this not in STRING_ENUMS:
                return _fail("unapproved_literal", f"string literal {lit.this!r} is not in the enum set")
        else:
            try:
                value = int(lit.this)
            except ValueError:
                return _fail("unapproved_literal", f"non-integer numeric literal {lit.this!r}")
            if abs(value) > 1_000_000_000:
                return _fail("unapproved_literal", f"numeric literal {value} exceeds 1e9")

    for select in tree.find_all(exp.Select):
        limit = select.args.get("limit")
        if limit is not None:
            expr = limit.expression if hasattr(limit, "expression") else None
            if not isinstance(expr, exp.Literal) or expr.is_string:
                return _fail("invalid_limit", "LIMIT must be an integer literal, not a parameter")
            value = int(expr.this)
            if not (1 <= value <= MAX_LIMIT):
                return _fail("invalid_limit", f"LIMIT {value} outside 1..{MAX_LIMIT}")
        offset = select.args.get("offset")
        if offset is not None:
            expr = offset.expression if hasattr(offset, "expression") else None
            if not isinstance(expr, exp.Literal) or expr.is_string:
                return _fail("invalid_offset", "OFFSET must be an integer literal")
            if not (0 <= int(expr.this) <= MAX_OFFSET):
                return _fail("invalid_offset", f"OFFSET outside 0..{MAX_OFFSET}")

    names = []
    for e in tree.expressions:
        # Unaliased aggregates/functions are legal; PostgreSQL names them.
        if isinstance(e, exp.Func) and not e.alias:
            continue
        names.append(e.alias_or_name)
    if any(n and not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", str(n)) for n in names):
        return _fail("invalid_projection", "projection aliases must match [a-z][a-z0-9_]{0,79}")
    nonempty = [n for n in names if n]
    if len(set(nonempty)) != len(nonempty):
        return _fail("duplicate_alias", "projection aliases must be unique")
    placeholders = []
    for ph in tree.find_all(exp.Placeholder):
        name = ph.name.lower()
        if not name:
            return _fail("invalid_parameters", "unnamed placeholder")
        if name not in ALLOWED_PARAMETERS:
            return _fail("invalid_parameters", f"parameter {name!r} is not allowed")
        placeholders.append(name)
    placeholders = sorted(set(placeholders))
    declared = sorted(set(parameter_names or []))
    if placeholders != declared:
        return _fail("invalid_parameters", f"SQL placeholders {placeholders} != declared {declared}")

    # A parameter may not be used as a literal position (LIMIT/ORDER index etc.)
    # is already covered for LIMIT/OFFSET; placeholders in ORDER BY are allowed
    # here only as a value, never as a column identity.
    for order in tree.find_all(exp.Ordered):
        if order.find(exp.Placeholder):
            return _fail("invalid_parameters", "parameter cannot appear in ORDER BY")

    staged = sql.strip()
    return Validation(
        True, "ok", "", placeholders, view_names, staged,
        hashlib.sha256(staged.encode("utf-8")).hexdigest(),
    )


def _depth(node, level: int = 0) -> int:
    best = level
    for child in node.iter_expressions():
        best = max(best, _depth(child, level + 1))
    return best


def to_psycopg(sql: str) -> str:
    """Render :name placeholders as psycopg named placeholders via AST.

    A textual regex would also rewrite ':' inside comments, strings and
    dollar-quoted bodies; the AST renderer only rewrites real placeholders.
    """
    return sqlglot.parse_one(sql, read="postgres").sql(dialect="postgres")


if __name__ == "__main__":
    import sys
    sample = sys.stdin.read()
    print(json.dumps(validate(sample).as_dict(), ensure_ascii=False, indent=2))
