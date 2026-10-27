"""ITER-20261025-01: bounded, pure business regressions; no PG/model/service."""
from datetime import UTC, datetime

import pytest

from cs_agent.aftersales import rules
from cs_agent.agent.facts import render_facts
from cs_agent.core.errors import AppError
from cs_agent.query.bindings import explicit_window
from cs_agent.query.validator import validate_query


@pytest.mark.parametrize("question", [
    "查2026年11月30日和12月的订单", "2026-11-30和2026年12月的订单",
    "2026年11月30日及第四季度订单", "2026-11-30和12月1日",
])
def test_day_cannot_swallow_additional_unimplemented_time(question):
    result = explicit_window(question, datetime(2026,12,5,tzinfo=UTC))
    assert result["kind"] == "clarify"
    assert result["reason_code"] == "invalid_or_ambiguous_time"


@pytest.mark.parametrize("tail", ["created_at<=:end_at", ":end_at>=created_at",
                                    "created_at BETWEEN :start_at AND :end_at"])
def test_inclusive_or_reversed_time_upper_bound_is_not_half_open(tail):
    sql = "SELECT order_id FROM cs_order_self WHERE created_at>=:start_at AND " + tail
    with pytest.raises(AppError):
        validate_query(sql)


@pytest.mark.parametrize("predicate", ["created_at>=:start_at AND created_at<:end_at",
    ":start_at<=created_at AND :end_at>created_at"])
def test_equivalent_half_open_predicates_remain_legal(predicate):
    assert validate_query("SELECT order_id FROM cs_order_self WHERE "+predicate).parameters == {"start_at","end_at"}


def test_one_unit_return_does_not_scan_entire_original_quantity(monkeypatch):
    visited = []
    def bounded_range(*args):
        for index in range(*args):
            visited.append(index)
            assert len(visited)<=8, "single-unit refund scanned the full order quantity"
            yield index
    monkeypatch.setattr(rules,"range",bounded_range,raising=False)
    line = {"ordered_qty":100000,"unit_allocations":{"quantity":100000,"base_minor":"1","tail_minor":"2"},
            "allocation_revision":"ALLOC-LAST-UNIT-v1","paid_goods_minor":100001,
            "reserved_refund_qty":0,"refunded_qty":0,"reserved_refund_minor":0,"refunded_goods_minor":0}
    assert rules.allocate_units(line,1,set()) == ([0],1)
    assert len(visited)<=1


@pytest.mark.parametrize("trade,fulfillment,expected", [
    ("awaiting_payment","unshipped","待付款"), ("cancelled_unpaid","unshipped","未付款已取消"),
    ("completed","received_by_customer","本人已确认收货"),
])
def test_current_business_enums_render_real_order_status(trade,fulfillment,expected):
    order = {"display_no":"SIM-TEST","trade_status":trade,"fulfillment_status":fulfillment,
             "paid_minor":"0","refunded_minor":"0"}
    answer = render_facts([{"name":"orders.detail","result":order}])
    assert expected in answer
    assert "待核对" not in answer
