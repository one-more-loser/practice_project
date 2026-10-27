"""COORD-01-R1-001: unsupported residual time must not narrow trusted windows."""
from datetime import UTC, datetime

import pytest

from cs_agent.query.bindings import explicit_window
from tests.unit.test_sql_call_chain import execute, graph, install_candidate, result  # noqa: F401

NOW = datetime(2026, 12, 5, tzinfo=UTC)
ORIGINAL_FAILURES = [
    "查2026年11月30日和前天的订单",
    "查2026年11月30日到前天的订单",
    "查2026年11月和前天的订单",
]
RELATED_NEGATIVES = [
    "查2026-11-30至前天的订单", "查前天和2026年11月的订单",
    "查2026年11月30日和大前天的订单", "查2026年11月和后天的订单",
    "查2026年11月和下周的订单", "查2026年11月30日到春节的订单",
    "查2026年11月30日的下午订单", "查2026年11月和月底的订单",
    "查本月和前天的订单", "查昨天到前天的订单", "查近30天和前天的订单",
    "查2026年11月30日至2026年12月1日以及前天的订单",
    "查2026年11月30日到未知日期的订单", "查前天的订单",
    "查2026年11月30日和12月的订单",
]


@pytest.mark.parametrize("question", ORIGINAL_FAILURES + RELATED_NEGATIVES)
def test_unconsumed_time_conditions_require_clarification(question):
    response = explicit_window(question, NOW)
    assert response["kind"] == "clarify"
    assert response["reason_code"] == "invalid_or_ambiguous_time"
    assert set(response["missing_slots"]) == {"start_at", "end_at"}


@pytest.mark.parametrize("question,start,end", [
    ("查2026年11月30日的订单", "2026-11-29T16:00:00+00:00", "2026-11-30T16:00:00+00:00"),
    ("查2026年11月的订单", "2026-10-31T16:00:00+00:00", "2026-11-30T16:00:00+00:00"),
    ("2026-11-30至2026-12-01", "2026-11-29T16:00:00+00:00", "2026-12-01T16:00:00+00:00"),
    ("2024年2月29日", "2024-02-28T16:00:00+00:00", "2024-02-29T16:00:00+00:00"),
    ("2026年12月31日至2027年1月1日", "2026-12-30T16:00:00+00:00", "2027-01-01T16:00:00+00:00"),
    ("本月的订单", "2026-11-30T16:00:00+00:00", "2026-12-31T16:00:00+00:00"),
    ("昨天的订单", "2026-12-03T16:00:00+00:00", "2026-12-04T16:00:00+00:00"),
    ("近30天的订单", "2026-11-05T00:00:00+00:00", "2026-12-05T00:00:00+00:00"),
    ("2024-01-01至2024-12-31", "2023-12-31T16:00:00+00:00", "2024-12-31T16:00:00+00:00"),
])
def test_supported_windows_remain_exact_shanghai_half_open(question, start, end):
    assert explicit_window(question, NOW) == {
        "start_at": datetime.fromisoformat(start), "end_at": datetime.fromisoformat(end)
    }


def test_window_longer_than_366_days_stays_rejected():
    response = explicit_window("2024-01-01至2025-01-01", NOW)
    assert response["kind"] == "clarify"
    assert response["reason_code"] == "invalid_or_ambiguous_time"


@pytest.mark.parametrize("question", ORIGINAL_FAILURES)
def test_original_failures_stop_before_gateway_or_executor(graph, question):  # noqa: F811
    install_candidate(graph, "SELECT order_id FROM cs_order_self WHERE created_at>=:start_at AND created_at<:end_at", ["start_at", "end_at"])
    response = result(execute(graph, question))
    assert response["reason_code"] == "invalid_or_ambiguous_time"
    assert graph.requests == [] and graph.calls == []
