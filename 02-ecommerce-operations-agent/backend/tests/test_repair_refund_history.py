"""R1退款日序列回归；仅fixture隔离SQLite，不接触演示库或真实HTTP。"""
from copy import deepcopy
from datetime import timedelta

import pytest
from test_domain import NOW, draft

pytest_plugins = ("test_joint_operations",)


def order(identity, paid_at, mixed=True, channel="web"):
    items = [{"sku_id": "A", "qty": 1, "paid_minor": "10000"}]
    if mixed:
        items.append({"sku_id": "B", "qty": 1, "paid_minor": "20000"})
    return {"order_id": identity, "paid_at": paid_at.isoformat(), "status": "paid",
            "channel": channel, "paid_minor": "30000" if mixed else "10000", "items": items}


def refund(identity, order_id, when, amount="5000", status="succeeded"):
    return {"refund_id": identity, "order_id": order_id, "occurred_at": when.isoformat(),
            "amount_minor": amount, "status": status}


def query(env, orders, refunds, *, start=None, end=None, watermark=None, sku="A", channel="web"):
    _, operations, principal, original = env
    source = deepcopy(original)
    source["revision"] = 2
    other = deepcopy(source["skus"][0])
    other["sku_id"] = "B"
    source["skus"].append(other)
    source.update(orders=orders, refunds=refunds)
    if watermark:
        source["as_of"] = watermark.isoformat()
    operations.import_snapshot(principal, source)
    return operations.dashboard(principal, source["snapshot_id"],
        (start or NOW-timedelta(days=4)).isoformat(), (end or NOW).isoformat(), sku, channel)


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("amount", [None, "0", "5000"])
def test_single_mixed_no_zero_and_positive_refund(setup, mixed, amount):
    when = NOW-timedelta(hours=1)
    result = query(setup, [order("O1", when-timedelta(hours=1), mixed)],
                   [] if amount is None else [refund("R1", "O1", when, amount)])
    expected = None if mixed and amount == "5000" else amount or "0"
    assert result["metrics"]["refund_minor"] == expected
    assert result["history"][0]["refund_minor"] == expected
    assert bool(result["history"][0].get("refund_missing")) == (expected is None)


def test_refund_only_day_and_multiday_known_amounts(setup):
    result = query(setup, [order("O1", NOW-timedelta(days=3)), order("O2", NOW-timedelta(days=2), False)],
        [refund("R1", "O1", NOW-timedelta(days=1)), refund("R2", "O2", NOW-timedelta(days=2, hours=-1))])
    by_day = {row["date"]: row for row in result["history"]}
    assert by_day["2026-11-07"]["refund_minor"] == "0"
    assert by_day["2026-11-08"]["refund_minor"] == "5000"
    assert by_day["2026-11-09"]["refund_minor"] is None
    assert by_day["2026-11-09"]["payment_minor"] == "0"
    assert by_day["2026-11-09"]["refund_missing"] == [{"refund_id": "R1", "order_id": "O1",
        "occurred_at": (NOW-timedelta(days=1)).isoformat(), "reason": "missing_sku_allocation"}]


@pytest.mark.parametrize("offset,visible,expected", [
    (-1, 0, "0"), (0, 0, None), (1, 0, None), (2, 0, "0"),
    (3, 0, "0"), (1, -2, "0"), (1, -1, None),
])
def test_cash_window_start_inclusive_end_exclusive_and_watermark(setup, offset, visible, expected):
    # 原支付在窗外，退款是否影响现金指标不取决于原支付日期。
    start = NOW-timedelta(days=1)
    event = start+timedelta(hours=offset)
    mark = NOW if visible == 0 else event+timedelta(hours=visible+1)
    result = query(setup, [order("O1", NOW-timedelta(days=3))], [refund("R1", "O1", event)],
                   start=start, end=start+timedelta(hours=2), watermark=mark)
    assert result["metrics"]["refund_minor"] == expected
    assert bool(result["history"]) == (expected is None)
    assert result["metrics"]["cohort_refund_order_rate"] is None  # 无支付批次，分母缺失
    assert bool(result["filter_missing"]) == (expected is None)


def test_future_event_after_visible_clock_does_not_pollute(setup):
    result = query(setup, [order("O1", NOW-timedelta(hours=2))],
                   [refund("R1", "O1", NOW+timedelta(hours=1))], end=NOW+timedelta(days=1))
    assert result["metrics"]["refund_minor"] == "0"
    assert result["metrics"]["cohort_refund_amount_rate"] == "0.000000"
    assert result["history"][0]["refund_minor"] == "0"
    assert not result["filter_missing"]


def test_visible_cohort_refund_outside_cash_window_only_affects_cohort(setup):
    result = query(setup, [order("O1", NOW-timedelta(days=2))],
        [refund("R1", "O1", NOW-timedelta(hours=1))], end=NOW-timedelta(days=1))
    assert result["metrics"]["refund_minor"] == "0"
    assert result["metrics"]["net_paid_minor"] == "10000"
    assert result["metrics"]["cohort_refund_amount_rate"] is None
    assert result["history"][0]["refund_minor"] == "0"


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_unsuccessful_refund_is_not_unknown(setup, status):
    result = query(setup, [order("O1", NOW-timedelta(hours=2))],
                   [refund("R1", "O1", NOW-timedelta(hours=1), status=status)])
    assert result["metrics"]["refund_minor"] == "0"
    assert not result["filter_missing"]


@pytest.mark.parametrize("sku,channel,expected", [(None, "web", "5000"), ("A", "other", "0"), ("B", "web", None)])
def test_unfiltered_amount_or_excluded_channel(setup, sku, channel, expected):
    result = query(setup, [order("O1", NOW-timedelta(hours=2)),
                          order("O2", NOW-timedelta(hours=2), False, "other")],
                   [refund("R1", "O1", NOW-timedelta(hours=1))], sku=sku, channel=channel)
    assert result["metrics"]["refund_minor"] == expected


def test_export_keeps_original_refund_facts_and_no_dashboard_projection(setup):
    query(setup, [order("O1", NOW-timedelta(hours=2))], [refund("R1", "O1", NOW-timedelta(hours=1))])
    _, operations, principal, _ = setup
    spec = draft()
    spec["expected_snapshot_revision"] = 2
    plan = operations.save_plan(principal, spec, "refund-export")
    for format_name in ("markdown", "csv"):
        text, _ = operations.export(principal, plan["plan_id"], format_name)
        assert '"refund_id": "R1"' in text.replace('""', '"')
        assert '"amount_minor": "5000"' in text.replace('""', '"')
        assert "refund_missing" not in text


def test_same_day_known_refund_cannot_mask_unknown_and_multiple_sources(setup):
    day = NOW-timedelta(days=1)
    result = query(setup, [order("O1", day-timedelta(days=1)), order("O2", day-timedelta(days=1), False)],
        [refund("R1", "O1", day), refund("R2", "O2", day), refund("R3", "O1", day, "2000")])
    row = result["history"][-1]
    assert row["refund_minor"] is None
    assert [source["refund_id"] for source in row["refund_missing"]] == ["R1", "R3"]


def test_zero_refund_only_day_is_complete_zero(setup):
    result = query(setup, [order("O1", NOW-timedelta(days=2))],
                   [refund("R1", "O1", NOW-timedelta(days=1), "0")])
    assert result["metrics"]["refund_minor"] == "0"
    assert result["history"][-1] == {"date": "2026-11-09", "payment_minor": "0", "refund_minor": "0"}
