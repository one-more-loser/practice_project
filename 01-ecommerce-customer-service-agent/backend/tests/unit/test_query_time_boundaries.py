"""COORD-01-R2-001: bounded date grammar and recoverable invalid continuations."""
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from cs_agent.query.bindings import explicit_window, prepare_slots
from tests.unit.test_sql_call_chain import (  # noqa: F401
    CTX,
    execute,
    graph,
    install_candidate,
    result,
)

NOW = datetime(2026, 12, 5, tzinfo=UTC)
ORIGINAL_FAILURES = ["查上上月的订单", "查非昨天的订单", "查大昨天的订单"]
RELATED_NEGATIVES = [
    "查上上个月的订单", "查大上月的订单", "查最近最近3天的订单",
    "查非 今天的订单", "查并不是在昨天创建的订单", "查不在今天创建的订单",
    "查排除昨天的订单", "查除了上月以外的订单", "查昨天以外的订单",
    "查昨天左右的订单", "查非2026年11月的订单", "查非2026-11-30的订单",
    "查不是时间为昨天的订单", "查大前天的订单",
]
SQL = ("SELECT i.order_id FROM cs_order_item_self i JOIN cs_order_self o "
       "ON i.order_id=o.order_id WHERE i.sku_id=:sku_id "
       "AND o.created_at>=:start_at AND o.created_at<:end_at")


@pytest.mark.parametrize("question", ORIGINAL_FAILURES + RELATED_NEGATIVES)
def test_modified_or_partial_date_is_not_a_trusted_window(question):
    response = explicit_window(question, NOW)
    assert response["kind"] == "clarify"
    assert response["reason_code"] == "invalid_or_ambiguous_time"
    assert set(response["missing_slots"]) == {"start_at", "end_at"}
    assert not {"start_at", "end_at"} & response.keys()


@pytest.mark.parametrize("question,start,end", [
    ("查昨天的订单", "2026-12-03T16:00:00+00:00", "2026-12-04T16:00:00+00:00"),
    ("查今天的订单", "2026-12-04T16:00:00+00:00", "2026-12-05T16:00:00+00:00"),
    ("查上月的订单", "2026-10-31T16:00:00+00:00", "2026-11-30T16:00:00+00:00"),
    ("查询本月的订单", "2026-11-30T16:00:00+00:00", "2026-12-31T16:00:00+00:00"),
    ("近7天的订单", "2026-11-28T00:00:00+00:00", "2026-12-05T00:00:00+00:00"),
    ("最近30天", "2026-11-05T00:00:00+00:00", "2026-12-05T00:00:00+00:00"),
    ("查昨天未支付的订单", "2026-12-03T16:00:00+00:00", "2026-12-04T16:00:00+00:00"),
    ("查未支付的昨天订单", "2026-12-03T16:00:00+00:00", "2026-12-04T16:00:00+00:00"),
    ("查昨天不是已支付状态的订单", "2026-12-03T16:00:00+00:00", "2026-12-04T16:00:00+00:00"),
    ("订单创建时间为昨天", "2026-12-03T16:00:00+00:00", "2026-12-04T16:00:00+00:00"),
    ("2026年11月查那笔订单", "2026-10-31T16:00:00+00:00", "2026-11-30T16:00:00+00:00"),
])
def test_ordinary_query_context_keeps_exact_windows(question, start, end):
    assert explicit_window(question, NOW) == {
        "start_at": datetime.fromisoformat(start), "end_at": datetime.fromisoformat(end)
    }


@pytest.mark.parametrize("question", ORIGINAL_FAILURES)
def test_prepare_slots_never_returns_or_retains_old_window(question):
    dialog = {}
    with Session() as session:
        values, clarify, _ = prepare_slots(session, CTX, "查上月订单", {}, dialog, NOW)
        assert not clarify and {"start_at", "end_at"} <= values.keys()
        dialog["sql_clarification"] = {"kind": "clarify", "missing_slots": ["sku_id"]}
        original = dialog["sql_binding"]["question"]
        supplement = "改为" + question
        values, clarify, actual_original = prepare_slots(session, CTX, supplement, {}, dialog, NOW)
    assert clarify["reason_code"] == "invalid_or_ambiguous_time"
    assert not {"start_at", "end_at"} & values.keys()
    assert not {"start_at", "end_at"} & dialog["sql_binding"]["values"].keys()
    assert actual_original == original
    assert dialog["sql_binding"]["supplements"] == [supplement]


@pytest.mark.parametrize("question", ORIGINAL_FAILURES)
def test_new_invalid_query_stops_real_graph_before_gateway_and_executor(graph, question):  # noqa: F811
    graph.clock.now = lambda: NOW
    install_candidate(graph, SQL, ["sku_id", "start_at", "end_at"])
    out = execute(graph, question)
    response = result(out)
    assert response["kind"] == "clarify"
    assert response["reason_code"] == "invalid_or_ambiguous_time"
    assert not {"start_at", "end_at"} & out["dialog_state"]["sql_binding"]["values"].keys()
    final = graph._assistant(out)
    assert final["waiting"]
    assert final["dialog_state"]["sql_clarification"]["missing_slots"] == ["end_at", "start_at"]
    assert graph.requests == [] and graph.calls == []


@pytest.mark.parametrize("question", ORIGINAL_FAILURES)
def test_invalid_continuation_blocks_old_window_then_recovers(graph, question):  # noqa: F811
    from copy import deepcopy

    from langchain_core.messages import HumanMessage

    graph.clock.now = lambda: NOW
    install_candidate(graph, SQL, ["sku_id", "start_at", "end_at"])
    original = "查上月订单"
    initial = graph._assistant(execute(graph, original))["dialog_state"]
    assert initial["sql_clarification"]["missing_slots"] == ["sku_id"]
    context_id = initial["sql_binding"]["context_id"]
    graph.requests.clear()
    supplement = "改为" + question
    routed = graph._assistant({"messages": [HumanMessage(content=supplement)], "dialog_state": initial})
    assert routed["actions"][0]["name"] == "sql.query"
    blocked = execute(graph, supplement, dialog=routed["dialog_state"])
    assert result(blocked)["reason_code"] == "invalid_or_ambiguous_time"
    assert graph.requests == [] and graph.calls == []
    pending = graph._assistant(blocked)["dialog_state"]
    binding = pending["sql_binding"]
    assert binding["context_id"] == context_id
    assert binding["question"] == original and binding["supplements"] == [supplement]
    assert not {"start_at", "end_at"} & binding["values"].keys()
    # 补SKU也不能复活原来的上月日期；仍需明确补充新时间。
    no_time = execute(graph, "sku_a", dialog=deepcopy(pending))
    assert result(no_time)["kind"] == "clarify"
    assert set(result(no_time)["missing_slots"]) == {"start_at", "end_at"}
    assert graph.calls == []
    assert graph.requests[-1]["query_context"]["explicit_time_window"] == {}
    # 该分支耗尽既有三轮澄清预算，原规则结束本次查询，不扩大轮数。
    assert graph._assistant(no_time)["dialog_state"] == {}
    # 另一个分支在预算内一次补齐日期和SKU，可以恢复原题执行。
    correction = "2026年11月30日的sku_a"
    recovered = execute(graph, correction, dialog=pending)
    assert "rows" in result(recovered)
    assert graph.calls[-1]["parameters"] == {
        "sku_id": "sku_a", "start_at": datetime(2026, 11, 29, 16, tzinfo=UTC),
        "end_at": datetime(2026, 11, 30, 16, tzinfo=UTC),
    }
    assert graph.requests[-1]["question"] == "\n用户补充：".join(
        [original, supplement, correction]
    )


def test_ordinary_unpaid_predicate_still_executes(graph):  # noqa: F811
    graph.clock.now = lambda: NOW
    install_candidate(graph, "SELECT order_id FROM cs_order_self WHERE trade_status=:status "
                      "AND created_at>=:start_at AND created_at<:end_at", ["status", "start_at", "end_at"])
    assert "rows" in result(execute(graph, "查昨天未支付的订单"))
    assert graph.calls[0]["parameters"]["status"] == "awaiting_payment"
