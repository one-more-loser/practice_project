import json
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, text

from cs_agent.config import Settings
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError
from cs_agent.db.session import verify_target
from cs_agent.query.schema import DIGEST, REVISION
from cs_agent.query.validator import date_range, rejected, validate_parameters, validate_query


class SQLExecutor:
    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.query_database_url:
            raise AppError("query_unavailable", 503, "受控查询尚未配置")
        self.engine = create_engine(
            settings.query_database_url.get_secret_value(),
            hide_parameters=True,
            pool_size=2,
            max_overflow=2,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": 3,
                "options": "-c statement_timeout=2000 -c lock_timeout=500 -c default_transaction_read_only=on -c timezone=UTC",
            },
        )
        verify_target(self.engine, settings, query=True)

    def query(self, ctx, sql, parameters, now, *, schema_revision=REVISION, schema_digest=DIGEST):
        if ctx.role != Role.CUSTOMER:
            raise AppError("forbidden", 403, "当前角色不允许本人自由查询")
        prepared = validate_query(sql, schema_revision=schema_revision, schema_digest=schema_digest)
        validate_parameters(prepared, parameters, now)
        budget = self.settings.query_plan_cost_limit
        if budget is None or not 0 < budget <= 100000:
            raise AppError("query_unavailable", 503, "查询成本预算尚未核定")
        try:
            with self.engine.connect() as connection, connection.begin():
                self.context(connection, ctx, now, parameters)
                plan = connection.execute(
                    text("EXPLAIN (FORMAT JSON) " + prepared.sql), parameters
                ).scalar_one()
                if isinstance(plan, str):
                    plan = json.loads(plan)
                cost = plan[0]["Plan"]["Total Cost"]
                if cost > budget:
                    raise rejected("query_cost_exceeded")
                rows = connection.execute(text(prepared.sql), parameters).mappings().fetchmany(51)
                if len(rows) > 50:
                    raise rejected("query_rows_exceeded")

                def value(item):
                    if isinstance(item, (datetime, date)):
                        return item.isoformat()
                    if isinstance(item, Decimal):
                        return str(item)
                    return item

                output = [
                    {
                        key: str(v) if key.endswith("_minor") and v is not None else value(v)
                        for key, v in row.items()
                    }
                    for row in rows
                ]
                if any(list(row) != prepared.columns for row in output):
                    raise rejected()
                return {
                    "rows": output,
                    "schema_revision": REVISION,
                    "schema_digest": DIGEST,
                    "plan_cost": float(cost),
                    "source": "controlled_postgresql",
                    "as_of": now.isoformat(),
                }
        except AppError:
            raise
        except Exception:
            raise AppError("query_unavailable", 503, "查询超时或暂不可用") from None

    @staticmethod
    def context(connection, ctx, now=None, parameters=None):
        connection.execute(text("SET LOCAL search_path=csq,pg_catalog"))
        window = date_range(
            now or datetime.now(UTC),
            (parameters or {}).get("start_at"),
            (parameters or {}).get("end_at"),
        )
        for key, value in {
            "owner": ctx.subject_id,
            "store": ctx.store_id,
            "batch": ctx.data_batch_id,
            "generation": str(ctx.batch_generation),
            "role": ctx.role.value,
            "start": window["start_at"].isoformat(),
            "end": window["end_at"].isoformat(),
        }.items():
            connection.execute(
                text("SELECT set_config(:name,:value,true)"), {"name": "cs." + key, "value": value}
            )

    def close(self):
        self.engine.dispose()
