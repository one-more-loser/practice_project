import os
from dataclasses import replace

import pytest
from sqlalchemy import text

from cs_agent.config import Settings
from cs_agent.core.context import Role, TrustedContext
from cs_agent.core.errors import AppError
from cs_agent.query.executor import SQLExecutor
from tests.api.test_checkout import create_order, key, propose_and_submit


def executor():
    return SQLExecutor(
        Settings(
            query_database_url=os.environ["CS_TEST_QUERY_DATABASE_URL"],
            expected_database="cs_agent_b0_test",
            query_plan_cost_limit=1000,
        )
    )


def customer(name):
    return TrustedContext(
        name, "store_sim", Role.CUSTOMER, "test_session", 1, "sim_b0_20261022", 1, "req_sql"
    )


def test_real_rls_omitted_where_reuse_and_column_permissions(environment, login_as):
    client, _, clock, *_ = environment
    headers = login_as("customer_a")
    a = create_order(client, headers)
    e = executor()
    try:
        assert e.query(
            customer("customer_a"), "SELECT order_id FROM cs_order_self", {}, clock.now()
        )["rows"] == [{"order_id": a}]
        assert (
            e.query(customer("customer_b"), "SELECT order_id FROM cs_order_self", {}, clock.now())[
                "rows"
            ]
            == []
        )
        with e.engine.connect() as connection:
            assert connection.execute(text("SELECT order_id FROM csq.cs_order_self")).all() == []
            assert connection.execute(text("SELECT current_setting('cs.owner',true)")).scalar() in {
                None,
                "",
            }
        for statement in [
            "SELECT phone FROM public.shipping_input",
            "SELECT cost_minor FROM public.sku",
            "SET ROLE cs_agent_app_b0",
            "DELETE FROM public.customer_order",
        ]:
            with e.engine.connect() as connection, pytest.raises(Exception):
                connection.execute(text(statement))
        with e.engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT rolsuper,rolbypassrls,rolcreaterole FROM pg_roles WHERE rolname=current_user"
                )
            ).one() == (False, False, False)
        with pytest.raises(AppError):
            e.query(
                replace(customer("customer_a"), role=Role.SUPPORT),
                "SELECT order_id FROM cs_order_self",
                {},
                clock.now(),
            )
    finally:
        e.close()


def test_result_semantics_preaggregation_and_cost_rejection(environment, login_as):
    client, _, clock, *_ = environment
    headers = login_as("customer_a")
    client.post(
        "/api/v1/me/topups",
        headers={**headers, **key()},
        json={"currency": "CNY", "amount_minor": "20000"},
    )
    ref = create_order(
        client,
        headers,
        [{"sku_id": "sku_cup_ivory", "qty": 2}, {"sku_id": "sku_cup_coral", "qty": 1}],
    )
    row = client.get("/api/v1/me/orders/" + ref).json()["data"]
    assert propose_and_submit(client, headers, "order.pay", ref, row["version"]).status_code == 200
    e = executor()
    try:
        sql = "WITH i AS (SELECT order_id,sum(paid_goods_minor) AS goods_minor FROM cs_order_item_self GROUP BY order_id) SELECT o.order_id,o.paid_minor,i.goods_minor FROM cs_order_self o JOIN i ON o.order_id=i.order_id"
        assert e.query(customer("customer_a"), sql, {}, clock.now())["rows"] == [
            {"order_id": ref, "paid_minor": "12200", "goods_minor": "12200"}
        ]
        e.settings.query_plan_cost_limit = 0.0001
        with pytest.raises(AppError, match="query_cost_exceeded"):
            e.query(customer("customer_a"), "SELECT order_id FROM cs_order_self", {}, clock.now())
    finally:
        e.close()
