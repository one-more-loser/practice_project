"""Offline contract observations. No engine, socket, business DB or paid model.

SQL validation is real; gateway uses MockTransport; executor/Agent dependencies
use explicit in-memory adapters. Results do not prove PostgreSQL execution.
"""
import hashlib
import json
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
from sqlglot import parse_one
from sqlglot.optimizer.qualify import qualify

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "backend/src/cs_agent").is_dir())
sys.path.insert(0, str(ROOT / "backend/src"))
from cs_agent.agent.graph import AgentGraph
from cs_agent.core.context import Role, TrustedContext
from cs_agent.core.errors import AppError
from cs_agent.models.gateway import ModelGateway, ServiceConfig
from cs_agent.query.executor import SQLExecutor
from cs_agent.query.metrics import METRICS
from cs_agent.query.schema import DIGEST, METRICS_REVISION, REVISION, SCHEMA
from cs_agent.query.validator import validate_query

OUT = Path(__file__).resolve().parent
PAID = "trade_status IN ('paid','completed','cancelled_refunded','closed_refunded') AND paid_minor>0"
WINDOW = "created_at>=:start_at AND created_at<:end_at"
cases = {
    "all_created_count": f"SELECT count(*) AS order_count FROM cs_order_self WHERE {WINDOW}",
    "paid_cohort_count": f"SELECT count(*) AS order_count FROM cs_order_self WHERE {WINDOW} AND {PAID}",
    "registered_sums": f"SELECT count(*) AS order_count,coalesce(sum(paid_minor),0) AS paid_minor,coalesce(sum(refunded_minor),0) AS refunded_minor,coalesce(sum(net_paid_minor),0) AS net_paid_minor FROM cs_order_self WHERE {WINDOW} AND {PAID}",
    "wallet_cash_refund": "SELECT coalesce(sum(amount_minor),0) AS refund_minor FROM cs_wallet_entry_self WHERE kind='refund' AND occurred_at>=:start_at AND occurred_at<:end_at",
    "sku_semijoin": f"SELECT sku_id,sum(paid_goods_minor) AS paid_goods_minor FROM cs_order_item_self WHERE order_id IN (SELECT order_id FROM cs_order_self WHERE {WINDOW} AND {PAID}) GROUP BY sku_id ORDER BY sku_id LIMIT 50",
    "order_sku_sum_join": "SELECT i.sku_id,sum(i.paid_goods_minor) AS paid_goods_minor FROM cs_order_item_self i JOIN cs_order_self o ON i.order_id=o.order_id GROUP BY i.sku_id",
    "preaggregate_join": "WITH i AS (SELECT order_id,sum(paid_goods_minor) AS goods_minor FROM cs_order_item_self GROUP BY order_id) SELECT o.order_id,o.paid_minor,i.goods_minor FROM cs_order_self o JOIN i ON o.order_id=i.order_id",
    "utc_day": f"SELECT date_trunc('day',created_at) AS bucket,count(*) AS order_count FROM cs_order_self WHERE {WINDOW} GROUP BY bucket ORDER BY bucket LIMIT 50",
    "utc_week": "SELECT date_trunc('week',created_at) AS bucket,count(*) AS order_count FROM cs_order_self GROUP BY date_trunc('week',created_at) ORDER BY bucket",
    "shanghai_at_time_zone": "SELECT date_trunc('day',created_at AT TIME ZONE 'Asia/Shanghai') AS bucket FROM cs_order_self",
    "shanghai_three_arg": "SELECT date_trunc('day',created_at,'Asia/Shanghai') AS bucket FROM cs_order_self",
    "three_arg_overloaded_status": "SELECT date_trunc('day',created_at,:status) AS bucket FROM cs_order_self",
    "shanghai_interval_shift": "SELECT date_trunc('day',created_at+INTERVAL '8 hours') AS bucket FROM cs_order_self",
    "amount_ratio_guarded": f"SELECT sum(refunded_minor)/sum(paid_minor) AS refund_amount_rate FROM cs_order_self WHERE {WINDOW} AND {PAID} HAVING sum(paid_minor)>0",
    "order_ratio_scalar": f"SELECT (SELECT count(*) FROM cs_order_self WHERE {WINDOW} AND {PAID} AND refunded_minor>0)*1.0/(SELECT count(*) FROM cs_order_self WHERE {WINDOW} AND {PAID}) AS refund_order_rate WHERE (SELECT count(*) FROM cs_order_self WHERE {WINDOW} AND {PAID})>0",
    "ratio_nullif": "SELECT sum(refunded_minor)/nullif(sum(paid_minor),0) AS refund_rate FROM cs_order_self",
    "old_original_paid": "SELECT original_paid_minor FROM cs_order_self",
    "old_row_limit": "SELECT order_id FROM cs_order_self LIMIT :row_limit",
    "colon_order": "SELECT order_id FROM cs_order_self WHERE order_id=:order_id",
    "psycopg_order": "SELECT order_id FROM cs_order_self WHERE order_id=%(order_id)s",
    "dollar_order": "SELECT order_id FROM cs_order_self WHERE order_id=$1",
    "question_order": "SELECT order_id FROM cs_order_self WHERE order_id=?",
    "id_literal": "SELECT order_id FROM cs_order_self WHERE order_id='order_A'",
    "limit_above": "SELECT order_id FROM cs_order_self LIMIT 99999",
    "limit_param_status": "SELECT order_id FROM cs_order_self LIMIT :status",
    "limit_zero": "SELECT order_id FROM cs_order_self LIMIT 0",
    "limit_decimal": "SELECT order_id FROM cs_order_self LIMIT 1.5",
    "offset_negative": "SELECT order_id FROM cs_order_self OFFSET -1",
    "offset_too_big": "SELECT order_id FROM cs_order_self OFFSET 10001",
    "left_shipments": "SELECT o.order_id,s.shipment_id FROM cs_order_self o LEFT JOIN cs_shipment_self s ON o.order_id=s.order_id WHERE s.shipment_id IS NULL",
    "count_join_not_grain_blocked": "SELECT count(*) AS order_count FROM cs_order_self o JOIN cs_order_item_self i ON o.order_id=i.order_id",
    "same_named_wrong_fk": "SELECT o.order_id FROM cs_order_self o JOIN cs_product_public p ON o.order_id=p.sku_id",
    "output_duplicate": "SELECT o.order_id,i.order_id FROM cs_order_self o JOIN cs_order_item_self i ON o.order_id=i.order_id",
    "write": "UPDATE cs_order_self SET trade_status='closed_refunded'",
    "star": "SELECT * FROM cs_order_self",
    "no_table_constant": "SELECT 1 AS order_count",
    "status_literal_other_column": "SELECT order_id FROM cs_order_self WHERE trade_status='topup'",
}
report = {"scope": "offline source-backed probes, no PostgreSQL/network/model execution",
          "versions": {"schema_revision": REVISION, "schema_digest": DIGEST, "metric_revision": METRICS_REVISION,
                       "sqlglot": version("sqlglot"), "SQLAlchemy": version("SQLAlchemy"), "python": sys.version.split()[0]},
          "schema": SCHEMA, "view_column_counts": {k: len(v) for k, v in SCHEMA.items()}, "metrics": METRICS,
          "validator": {}}
for label, sql in cases.items():
    try:
        prepared = validate_query(sql)
        report["validator"][label] = {"accepted": True, "sql_in": sql, "sql_out": prepared.sql,
                                      "parameters": sorted(prepared.parameters), "columns": prepared.columns}
    except AppError as exc:
        report["validator"][label] = {"accepted": False, "sql_in": sql, "error_code": exc.code, "http_status": exc.status}

NOW = datetime(2026, 12, 10, tzinfo=UTC)
CTX = TrustedContext("synthetic_customer", "synthetic_store", Role.CUSTOMER, "synthetic_session", 1, "fixture_v1", 1, "offline")
base_candidate = {"contract_version": "XCS-SQL-v0.1", "kind": "query", "schema_revision": REVISION,
                  "schema_digest": DIGEST, "metric_revision": METRICS_REVISION,
                  "sql": cases["all_created_count"], "parameter_names": ["start_at", "end_at"],
                  "service": {"model_identifier": "fixture-sql", "revision": "fixture-v1"}}
gateway_requests = []
gateway = ModelGateway({"sql": ServiceConfig(base_url="http://sql.test", model="fixture-sql", service_revision="fixture-v1", protocol="project-sql")},
                       transport=httpx.MockTransport(lambda request: (gateway_requests.append(json.loads(request.content)), httpx.Response(200, json=base_candidate))[1]))
gateway.invoke("sql", {"question": "2026年11月创建了多少订单"})
report["actual_project_sql_request"] = gateway_requests[0]


@contextmanager
def guard():
    yield (None, (None, None, SimpleNamespace(context=CTX)))


def run_graph(candidate):
    graph = object.__new__(AgentGraph)
    graph._guard = guard
    graph.clock = SimpleNamespace(now=lambda: NOW)
    graph.deadline = time.monotonic() + 60
    graph.gateway = SimpleNamespace(invoke=lambda *a, **k: candidate)
    captured = {}

    def execute(ctx, sql, parameters, now, **kwargs):
        captured.update(parameters={key: val.isoformat() for key, val in parameters.items()}, as_of=now.isoformat())
        return {"rows": [], "captured": captured}

    graph.query_service = SimpleNamespace(execute_candidate=execute)
    return graph._sql_query("请查询2026年11月，不是最近30天")


report["graph"] = {"absolute_month_question": run_graph(base_candidate),
    "order_id_parameter": run_graph({**base_candidate, "parameter_names": ["order_id"]}),
    "structured_clarify": run_graph({**base_candidate, "kind": "clarify", "clarify": {"slots": ["time_window"]}})}


class Rows:
    def __init__(self, data): self.data = data
    def scalar_one(self): return [{"Plan": {"Total Cost": 10}}]
    def mappings(self): return self
    def fetchmany(self, limit): return self.data[:limit]


class Connection:
    def __init__(self): self.seen = []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def begin(self): return self
    def execute(self, stmt, params=None):
        self.seen.append({"sql": str(stmt), "parameters": params})
        return Rows([{"paid_minor": 100, "average": Decimal("1.25"), "paid_at": None}])


connection = Connection()
executor = object.__new__(SQLExecutor)
executor.settings = SimpleNamespace(query_plan_cost_limit=1000)
executor.engine = SimpleNamespace(connect=lambda: connection)
query_sql = "SELECT paid_minor,avg(paid_minor) AS average,paid_at FROM cs_order_self GROUP BY paid_minor,paid_at"
report["executor_adapter"] = executor.query(CTX, query_sql, {}, NOW)
report["executor_context_keys"] = [row["parameters"]["name"] for row in connection.seen if row["parameters"] and "name" in row["parameters"]]
for label, sql, parameters in [
    ("extra_param", "SELECT order_id FROM cs_order_self", {"status": "paid"}),
    ("missing_param", cases["colon_order"], {}),
    ("unpaired_time", "SELECT order_id FROM cs_order_self WHERE created_at>=:start_at", {"start_at": NOW}),
    ("string_times", cases["all_created_count"], {"start_at": "2026-11-01T00:00:00+08:00", "end_at": "2026-12-01T00:00:00+08:00"}),
]:
    try:
        executor.query(CTX, sql, parameters, NOW)
        report.setdefault("executor_parameter_errors", {})[label] = "unexpected_acceptance"
    except AppError as exc:
        report.setdefault("executor_parameter_errors", {})[label] = exc.code

shanghai = ZoneInfo("Asia/Shanghai")
report["time_boundaries"] = [{"local": value, "utc": datetime.fromisoformat(value).astimezone(UTC).isoformat()}
    for value in ["2026-11-01T00:00:00+08:00", "2026-12-01T00:00:00+08:00", "2026-11-02T00:00:00+08:00"]]
report["enum_count"] = len(__import__("cs_agent.query.validator", fromlist=["ENUMS"]).ENUMS)
store_scope_query = "SELECT o.order_id FROM customer_order o WHERE EXISTS (SELECT 1 FROM store s WHERE s.store_id=store_id AND s.status='active')"
report["rls_store_scope_observation"] = {
    "scope": "SQLGlot lexical qualification only, not PG policy execution",
    "original_select": store_scope_query,
    "qualified_select": qualify(parse_one(store_scope_query, read="postgres"), dialect="postgres",
        schema={"customer_order": {"order_id": "TEXT", "store_id": "TEXT"},
                "store": {"store_id": "TEXT", "status": "TEXT"}}).sql(dialect="postgres"),
    "status": "needs isolated PG verification; do not assert inactive-store RLS test passed",
}
assert sum(report["view_column_counts"].values()) == 52
assert report["validator"]["wallet_cash_refund"]["accepted"]
assert report["graph"]["absolute_month_question"]["captured"]["parameters"]["end_at"].startswith("2026-12-10")
(OUT / "offline-observations.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"versions": report["versions"], "view_columns": report["view_column_counts"], "enum_count": report["enum_count"],
                  "validator_outcomes": {key: value.get("error_code", "accepted") for key, value in report["validator"].items()},
                  "graph": report["graph"], "executor_parameter_errors": report["executor_parameter_errors"]}, ensure_ascii=False))
