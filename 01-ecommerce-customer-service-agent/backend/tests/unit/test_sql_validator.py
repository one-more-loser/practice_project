from datetime import UTC, datetime, timedelta

import pytest

from cs_agent.core.errors import AppError


def validate(sql, **kwargs):
    from cs_agent.query.validator import validate_query

    return validate_query(sql, **kwargs)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM cs_order_self",
        "SELECT order_id FROM cs_order_self; SELECT 1",
        "WITH x AS (DELETE FROM cs_order_self RETURNING order_id) SELECT order_id FROM x",
        "SELECT * FROM cs_order_self",
        "SELECT o.* FROM cs_order_self o",
        "SELECT pg_sleep(10)",
        "SELECT set_config('cs.owner','customer_b',true)",
        "SELECT phone FROM shipping_input",
        "SELECT cost_minor FROM cs_product_public",
        "SELECT order_id FROM public.cs_order_self",
        "SELECT order_id INTO copied FROM cs_order_self",
        "SELECT order_id FROM cs_order_self FOR UPDATE",
        "SELECT x FROM generate_series(1,10000000) x",
        "SELECT order_id FROM cs_order_self WHERE order_id=:owner",
        "SELECT order_id FROM cs_order_self WHERE order_id='customer_b'",
        "WITH RECURSIVE x AS (SELECT order_id FROM x) SELECT order_id FROM x",
        "SELECT sum(o.paid_minor) FROM cs_order_self o JOIN cs_order_item_self i ON i.order_id=o.order_id",
        "SELECT order_id AS phone FROM cs_order_self",
        "SELECT count(*) FROM pg_catalog.pg_class",
    ],
)
def test_dangerous_or_unauthorized_sql_is_rejected(sql):
    with pytest.raises(AppError):
        validate(sql)


def test_scopes_fields_parameters_and_aggregate_projection():
    prepared = validate(
        "WITH x AS (SELECT order_id, paid_minor FROM cs_order_self "
        "WHERE created_at>=:start_at AND created_at<:end_at) "
        "SELECT count(*) AS order_count, coalesce(sum(paid_minor),0) AS paid_minor FROM x"
    )
    assert prepared.parameters == {"start_at", "end_at"}
    assert prepared.columns == ["order_count", "paid_minor"]
    assert "LIMIT 20" in prepared.sql
    with pytest.raises(AppError):
        validate("WITH x AS (SELECT order_id FROM cs_order_self) SELECT price_minor FROM x")


def test_row_and_date_budgets_are_independent_of_model():
    assert "LIMIT 50" in validate("SELECT order_id FROM cs_order_self LIMIT 99999").sql
    from cs_agent.query.validator import date_range

    end = datetime(2026, 10, 22, tzinfo=UTC)
    assert date_range(end, None, None)["start_at"] == end - timedelta(days=30)
    with pytest.raises(AppError):
        date_range(end, end - timedelta(days=367), end)
    with pytest.raises(AppError):
        date_range(end, end, end - timedelta(days=1))
