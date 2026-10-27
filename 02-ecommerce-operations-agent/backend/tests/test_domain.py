"""金额期、候选约束与完整方案的纯领域验收；样本均为合成数据。"""

import importlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 10, 12, tzinfo=TZ)


def domain():
    try:
        return importlib.import_module("ops_agent.domain")
    except ModuleNotFoundError:
        pytest.fail("RED: 纯领域模块尚未实现")


def snapshot():
    return {
        "snapshot_id": "snapshot_gold", "revision": 1, "store_id": "store_A",
        "source_kind": "synthetic", "as_of": NOW.isoformat(), "currency": "CNY",
        "skus": [{"sku_id": "A", "title": "商品A", "category": "服装",
                  "base_price_minor": "10000", "cost_minor": "6000",
                  "variable_fee_minor": "500", "available_qty": 120,
                  "price_version": 1}],
        "orders": [], "refunds": [],
    }


def draft():
    return {
        "title": "合成直降方案", "snapshot_id": "snapshot_gold",
        "expected_snapshot_revision": 1,
        "start_at": "2026-11-11T00:00:00+08:00",
        "end_at": "2026-11-12T00:00:00+08:00", "selected_candidate": "direct",
        "fixed_fee_minor": "10000",
        "goal": {"min_revenue_minor": "900000", "min_profit_minor": "250000",
                 "budget_minor": "100000", "scenario_policy": "base"},
        "lines": [{"sku_id": "A", "quantities": {"low": 80, "base": 100, "high": 120},
                   "direct_price_minor": "9200", "basket_qty": 1,
                   "coupon_minor": "800", "coupon_threshold_minor": "10000"}],
        "resources": [
            {"role": role, "volume_per_unit": "0.3", "productivity_per_hour": "20",
             "people": 1, "shift_hours": "8", "attendance": "1", "efficiency": "0.75",
             "source": "模拟排班v1", "confirmed": True}
            for role in ("客服", "仓储")
        ],
        "tasks": [
            {"title": "发布前复核", "owner": "店长", "stage": "before_publish",
             "due_at": "2026-11-10T18:00:00+08:00", "done": True},
            {"title": "活动值守", "owner": "客服班组", "stage": "during",
             "due_at": "2026-11-11T00:00:00+08:00", "done": False},
            {"title": "活动复盘", "owner": "店长", "stage": "after",
             "due_at": "2026-11-12T12:00:00+08:00", "done": False},
        ],
        "monitoring": {"interval_minutes": 5, "data_source": "01.operations_snapshot",
                       "stop_conditions": ["预算不足时停止新增优惠订单"]},
        "notes": "合成销量情景，不是预测；无退款假设。",
    }


def candidate(result, candidate_id):
    return next(row for row in result["candidates"] if row["id"] == candidate_id)


def order(order_id, paid_at, amount="30000", status="paid", channel="自然流量"):
    return {"order_id": order_id, "paid_at": paid_at, "status": status,
            "channel": channel, "paid_minor": amount,
            "items": [{"sku_id": "A", "qty": 1, "paid_minor": "10000"},
                      {"sku_id": "A", "qty": 2, "paid_minor": str(int(amount) - 10000)}]}


def refund(refund_id, order_id, occurred_at, amount, status="succeeded"):
    return {"refund_id": refund_id, "order_id": order_id, "occurred_at": occurred_at,
            "status": status, "amount_minor": amount}


def metrics_fixture():
    value = snapshot()
    value["orders"] = [
        order("O0", "2026-10-01T10:00:00+08:00"),
        order("O1", "2026-11-01T00:00:00+08:00"),
        order("O2", None, "20000", "unpaid"),
        order("O3", "2026-11-10T12:00:00+08:00", "10000", channel=""),
        order("O4", "2026-11-11T00:00:00+08:00", "10000"),
    ]
    value["refunds"] = [
        refund("R1", "O1", "2026-11-02T10:00:00+08:00", "10000"),
        refund("R2", "O0", "2026-11-03T10:00:00+08:00", "5000"),
        refund("R3", "O1", "2026-11-03T10:00:00+08:00", "2000", "pending"),
        refund("R4", "O1", "2026-11-10T18:00:00+08:00", "5000"),
    ]
    return value


def test_cash_period_and_payment_cohort_are_distinct_without_order_line_fanout():
    value = metrics_fixture()
    result = domain().calculate_metrics(
        value, datetime(2026, 11, 1, tzinfo=TZ), datetime(2026, 11, 10, tzinfo=TZ), NOW
    )
    assert result["payment_minor"] == "30000"
    assert result["refund_minor"] == "15000"
    assert result["net_paid_minor"] == "15000"
    assert result["paid_order_count"] == 1
    assert result["paid_units"] == 3
    assert result["average_order_minor"] == "30000"
    assert result["cohort_refund_order_rate"] == "1.000000"
    assert result["cohort_refund_amount_rate"] == "0.333333"
    assert result["channels"] == [
        {"channel": "自然流量", "payment_minor": "30000", "paid_order_count": 1}
    ]
    assert "conversion_rate" not in result
    assert "gross_profit_minor" not in result


def test_metrics_deduplicate_identical_events_and_do_not_mutate_input():
    value = metrics_fixture()
    value["orders"].append(deepcopy(value["orders"][1]))
    value["refunds"].append(deepcopy(value["refunds"][0]))
    original = deepcopy(value)
    result = domain().calculate_metrics(value, datetime(2026, 11, 1, tzinfo=TZ), NOW, NOW)
    assert result["payment_minor"] == "30000"
    assert result["refund_minor"] == "15000"
    assert value == original


def test_metrics_as_of_is_inclusive_and_window_end_is_exclusive():
    result = domain().calculate_metrics(
        metrics_fixture(), datetime(2026, 11, 1, tzinfo=TZ),
        datetime(2026, 11, 11, tzinfo=TZ), NOW
    )
    assert result["payment_minor"] == "40000"
    assert result["paid_order_count"] == 2
    assert result["channels"][0] == {
        "channel": "unknown", "payment_minor": "10000", "paid_order_count": 1
    }


def test_metrics_do_not_read_beyond_snapshot_watermark():
    result = domain().calculate_metrics(
        metrics_fixture(), datetime(2026, 11, 1, tzinfo=TZ),
        datetime(2026, 11, 12, tzinfo=TZ), NOW + timedelta(days=2)
    )
    assert result["payment_minor"] == "40000"
    assert result["refund_minor"] == "15000"


def test_empty_denominators_stay_unknown_and_cash_refunds_can_be_negative():
    value = metrics_fixture()
    result = domain().calculate_metrics(
        value, datetime(2026, 11, 3, tzinfo=TZ), datetime(2026, 11, 4, tzinfo=TZ), NOW
    )
    assert result["net_paid_minor"] == "-5000"
    assert result["average_order_minor"] is None
    assert result["cohort_refund_order_rate"] is None
    assert result["cohort_refund_amount_rate"] is None


def test_cohort_refunds_after_cash_window_are_visible_at_as_of():
    value = metrics_fixture()
    value["refunds"][0]["occurred_at"] = "2026-11-05T10:00:00+08:00"
    result = domain().calculate_metrics(
        value, datetime(2026, 11, 1, tzinfo=TZ), datetime(2026, 11, 2, tzinfo=TZ), NOW
    )
    assert result["refund_minor"] == "0"
    assert result["cohort_refund_order_rate"] == "1.000000"
    assert result["cohort_refund_amount_rate"] == "0.333333"


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(currency="USD"),
    lambda x: x.update(revision=True),
    lambda x: x["skus"][0].update(base_price_minor="1.5"),
    lambda x: x["skus"][0].update(cost_minor="-1"),
    lambda x: x["skus"][0].update(available_qty=-1),
    lambda x: x["skus"][0].update(price_version=0),
    lambda x: x.update(as_of="2026-11-10T12:00:00"),
    lambda x: x.update(customer_phone="不应接收个人信息"),
])
def test_invalid_snapshot_is_rejected_atomically(mutation):
    module = domain()
    value = snapshot()
    mutation(value)
    with pytest.raises(module.DomainError) as error:
        module.validate_snapshot(value)
    assert error.value.code == "invalid_snapshot"


@pytest.mark.parametrize("mutation", [
    lambda x: x["orders"].append({**deepcopy(x["orders"][0]), "paid_minor": "99999"}),
    lambda x: x["refunds"].append({**deepcopy(x["refunds"][0]), "amount_minor": "1"}),
    lambda x: x["refunds"][0].update(order_id="not_found"),
    lambda x: x["refunds"][0].update(amount_minor="999999"),
    lambda x: x["orders"][0]["items"][0].update(sku_id="not_found"),
    lambda x: x["orders"][0]["items"][0].update(qty=-1),
    lambda x: x["orders"][0]["items"][0].update(paid_minor="1"),
])
def test_snapshot_rejects_conflicting_duplicate_or_broken_event_reconciliation(mutation):
    module = domain()
    value = metrics_fixture()
    mutation(value)
    with pytest.raises(module.DomainError):
        module.validate_snapshot(value)


def test_snapshot_deduplicates_and_preserves_unknown_cost():
    value = metrics_fixture()
    value["skus"][0]["cost_minor"] = None
    value["orders"].append(deepcopy(value["orders"][0]))
    result = domain().validate_snapshot(value)
    assert len(result["orders"]) == 5
    assert result["skus"][0]["cost_minor"] is None
    assert len(value["orders"]) == 6


def test_gold_amounts_do_not_subtract_discount_twice_and_direct_is_locally_feasible():
    result = domain().evaluate_plan(draft(), snapshot(), NOW)
    baseline = candidate(result, "baseline")["scenarios"]["base"]
    direct = candidate(result, "direct")["scenarios"]["base"]
    coupon = candidate(result, "coupon")["scenarios"]["base"]
    assert (baseline["revenue_minor"], baseline["gross_profit_minor"],
            baseline["contribution_profit_minor"], baseline["budget_used_minor"]) == (
                "1000000", "400000", "340000", "10000")
    for row in (direct, coupon):
        assert (row["revenue_minor"], row["gross_profit_minor"],
                row["contribution_profit_minor"], row["budget_used_minor"]) == (
                    "920000", "320000", "260000", "90000")
    assert result["feasibility"] == "checks_passed"
    assert candidate(result, "baseline")["rule_supported"] is False
    assert result["lineage"]["snapshot_revision"] == 1
    assert "01" in result["lineage"]["validation_scope"]


def test_ninety_percent_discount_fails_profit_and_budget():
    value = draft()
    value["lines"][0]["direct_price_minor"] = "9000"
    result = domain().evaluate_plan(value, snapshot(), NOW)
    row = candidate(result, "direct")["scenarios"]["base"]
    assert row["contribution_profit_minor"] == "240000"
    assert row["budget_used_minor"] == "110000"
    assert {check["code"] for check in row["checks"] if check["status"] == "fail"} >= {
        "profit", "budget"
    }
    assert result["feasibility"] == "infeasible"


def test_budget_and_profit_equality_pass_but_one_cent_over_budget_fails():
    value = draft()
    value["goal"].update(budget_minor="90000", min_profit_minor="260000")
    assert domain().evaluate_plan(value, snapshot(), NOW)["feasibility"] == "checks_passed"
    value["goal"]["budget_minor"] = "89999"
    assert domain().evaluate_plan(value, snapshot(), NOW)["feasibility"] == "infeasible"


@pytest.mark.parametrize("field", ["cost_minor", "variable_fee_minor", "available_qty"])
def test_missing_hard_constraint_data_never_becomes_zero_or_ready(field):
    source = snapshot()
    source["skus"][0][field] = None
    result = domain().evaluate_plan(draft(), source, NOW)
    row = candidate(result, "direct")["scenarios"]["base"]
    assert result["feasibility"] == "needs_data"
    assert any(check["status"] == "unknown" for check in row["checks"])
    if field != "available_qty":
        assert row["contribution_profit_minor"] is None


def test_cost_missing_does_not_erase_known_revenue_and_fee_missing_keeps_gross_profit():
    source = snapshot()
    source["skus"][0]["variable_fee_minor"] = None
    row = candidate(domain().evaluate_plan(draft(), source, NOW), "direct")["scenarios"]["base"]
    assert row["revenue_minor"] == "920000"
    assert row["gross_profit_minor"] == "320000"
    assert row["contribution_profit_minor"] is None


def test_high_scenario_risk_is_preserved_and_all_policy_blocks_confirmation():
    value = draft()
    value["lines"][0]["quantities"]["high"] = 130
    result = domain().evaluate_plan(value, snapshot(), NOW)
    assert result["feasibility"] == "checks_passed"
    assert any(c["status"] == "fail" for c in candidate(result, "direct")["scenarios"]["high"]["checks"])
    value["goal"]["scenario_policy"] = "all"
    assert domain().evaluate_plan(value, snapshot(), NOW)["feasibility"] == "infeasible"


def test_coupon_support_cannot_be_self_asserted_by_snapshot():
    value, source = draft(), snapshot()
    value["selected_candidate"] = "coupon"
    source["execution_supported_rules"] = ["coupon", "direct"]
    result = domain().evaluate_plan(value, source, NOW)
    assert candidate(result, "coupon")["calculation_passed"] is True
    assert candidate(result, "coupon")["rule_supported"] is False
    assert result["feasibility"] != "checks_passed"
    assert domain().evaluate_plan(value, source, NOW, ["coupon"])["feasibility"] == "checks_passed"


def test_baseline_can_compare_but_cannot_be_published_as_direct():
    value = draft()
    value["selected_candidate"] = "baseline"
    result = domain().evaluate_plan(value, snapshot(), NOW, ["baseline", "direct"])
    assert candidate(result, "baseline")["calculation_passed"] is True
    assert result["feasibility"] != "checks_passed"


def test_coupon_threshold_is_inclusive_and_unqualified_basket_gets_no_discount():
    value = draft()
    value["selected_candidate"] = "coupon"
    value["lines"][0]["coupon_threshold_minor"] = "10001"
    result = domain().evaluate_plan(value, snapshot(), NOW)
    row = candidate(result, "coupon")["scenarios"]["base"]
    assert row["revenue_minor"] == "1000000"
    assert row["budget_used_minor"] == "10000"
    assert any(c["code"].startswith("coupon_eligibility") and c["status"] == "fail" for c in row["checks"])


def test_multi_sku_baskets_are_independent_and_never_combine_thresholds():
    value, source = draft(), snapshot()
    second = deepcopy(source["skus"][0])
    second.update(sku_id="B", base_price_minor="6000", cost_minor="3000", available_qty=200)
    source["skus"].append(second)
    value["lines"].append({"sku_id": "B", "quantities": {"low": 80, "base": 100, "high": 120},
                           "direct_price_minor": "5500", "basket_qty": 1,
                           "coupon_minor": "800", "coupon_threshold_minor": "10000"})
    row = candidate(domain().evaluate_plan(value, source, NOW), "coupon")["scenarios"]["base"]
    assert row["revenue_minor"] == "1520000"
    assert row["budget_used_minor"] == "90000"


def test_resource_capacity_uses_effective_hours_and_ceiling_additional_people():
    value = draft()
    value["resources"][0].update(volume_per_unit="2", productivity_per_hour="10")
    result = domain().evaluate_plan(value, snapshot(), NOW)
    resource = next(row for row in result["resources"] if row["role"] == "客服")
    assert resource["required_hours"] == "20"
    assert resource["available_hours"] == "6"
    assert resource["gap_hours"] == "14"
    assert resource["additional_people"] == 3
    assert result["feasibility"] == "infeasible"


def test_confirmed_shift_cannot_create_capacity_outside_activity_window():
    value = draft()
    value["resources"][0].update(
        volume_per_unit="1", productivity_per_hour="2", people=1,
        shift_hours="100", attendance="1", efficiency="1", confirmed=True,
    )
    result = domain().evaluate_plan(value, snapshot(), NOW)
    resource = next(row for row in result["resources"] if row["role"] == "客服")
    assert result["feasibility"] == "infeasible"
    assert resource["required_hours"] == "50"
    assert resource["available_hours"] == "24"
    assert resource["gap_hours"] == "26"
    assert any(check["code"] == "resource:客服" and check["status"] == "fail"
               for check in result["lineage"]["checks"])


def test_shift_equal_to_activity_duration_is_valid_but_any_excess_fails():
    value = draft()
    value["resources"][0]["shift_hours"] = "24"
    assert domain().evaluate_plan(value, snapshot(), NOW)["feasibility"] == "checks_passed"
    value["resources"][0]["shift_hours"] = "24.000001"
    assert domain().evaluate_plan(value, snapshot(), NOW)["feasibility"] == "infeasible"


@pytest.mark.parametrize("policy,expected", [
    ("base", "checks_passed"), ("high", "infeasible"), ("all", "infeasible"),
])
def test_all_candidate_scenarios_expose_resource_risks_independent_of_policy(policy, expected):
    value = draft()
    value["goal"].update(scenario_policy=policy, min_revenue_minor="0",
                         min_profit_minor="0", budget_minor="99999999")
    value["resources"][0].update(
        volume_per_unit="1", productivity_per_hour="20", people=1,
        shift_hours="5", attendance="1", efficiency="1",
    )
    result = domain().evaluate_plan(value, snapshot(), NOW)
    assert result["feasibility"] == expected
    for item in result["candidates"]:
        for name, required, gap in (("low", "4", "0"), ("base", "5", "0"), ("high", "6", "1")):
            scenario = item["scenarios"][name]
            resource = next(row for row in scenario["resources"] if row["role"] == "客服")
            assert resource["required_hours"] == required
            assert resource["available_hours"] == "5"
            assert resource["gap_hours"] == gap
            assert resource["additional_people"] == (1 if name == "high" else 0)
            check = next(row for row in scenario["resource_checks"] if row["code"] == "resource:客服")
            assert check["status"] == ("fail" if name == "high" else "pass")
    resource = next(row for row in result["resources"] if row["role"] == "客服")
    assert resource["gap_hours"] == ("0" if policy == "base" else "1")


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(resources=[]),
    lambda x: x["resources"].pop(),
    lambda x: x["resources"][0].update(confirmed=False),
    lambda x: x["resources"][0].update(source=""),
    lambda x: x["resources"][0].update(productivity_per_hour=None),
    lambda x: x.update(tasks=[]),
    lambda x: x["tasks"].pop(),
    lambda x: x["tasks"][0].update(owner=""),
    lambda x: x["monitoring"].update(data_source=""),
    lambda x: x["monitoring"].update(stop_conditions=[]),
    lambda x: x["goal"].update(min_profit_minor=None),
])
def test_missing_resource_task_or_monitoring_keeps_plan_needing_data(mutation):
    value = draft()
    mutation(value)
    assert domain().evaluate_plan(value, snapshot(), NOW)["feasibility"] == "needs_data"


@pytest.mark.parametrize("mutation", [
    lambda x: x["tasks"][0].update(done=False),
    lambda x: x["tasks"][0].update(due_at="2026-11-11T01:00:00+08:00"),
    lambda x: x["tasks"][1].update(due_at="2026-11-12T00:00:00+08:00"),
    lambda x: x["tasks"][2].update(due_at="2026-11-11T12:00:00+08:00"),
    lambda x: x["resources"][0].update(people=0),
])
def test_unfinished_prerequisite_or_invalid_schedule_or_no_capacity_blocks_plan(mutation):
    value = draft()
    mutation(value)
    assert domain().evaluate_plan(value, snapshot(), NOW)["feasibility"] == "infeasible"


@pytest.mark.parametrize("mutation", [
    lambda x: x["lines"][0].update(direct_price_minor="-1"),
    lambda x: x["lines"][0].update(direct_price_minor="10001"),
    lambda x: x["lines"][0].update(coupon_minor="10001"),
    lambda x: x["lines"][0].update(basket_qty=0),
    lambda x: x["lines"][0].update(coupon_threshold_minor="-1"),
    lambda x: x["lines"][0]["quantities"].update(base=-1),
    lambda x: x["lines"][0]["quantities"].update(low=101),
    lambda x: x["lines"].append(deepcopy(x["lines"][0])),
    lambda x: x["lines"][0].update(sku_id="absent"),
    lambda x: x["lines"][0].update(basket_skus=["A", "B"]),
    lambda x: x["resources"][0].update(productivity_per_hour="0"),
    lambda x: x["resources"][0].update(attendance="1.01"),
    lambda x: x["resources"][0].update(efficiency="NaN"),
    lambda x: x["monitoring"].update(interval_minutes=0),
])
def test_invalid_draft_numbers_rules_or_denominators_are_rejected(mutation):
    module = domain()
    value = draft()
    mutation(value)
    with pytest.raises(module.DomainError) as error:
        module.evaluate_plan(value, snapshot(), NOW)
    assert error.value.code == "invalid_draft"


def test_snapshot_revision_mismatch_is_conflict_even_if_amounts_are_same():
    module = domain()
    value = draft()
    value["expected_snapshot_revision"] = 2
    with pytest.raises(module.DomainError) as error:
        module.evaluate_plan(value, snapshot(), NOW)
    assert error.value.code == "snapshot_revision_conflict"


def test_snapshot_freshness_boundary_and_future_snapshot_never_pass():
    source = snapshot()
    source["as_of"] = (NOW - timedelta(hours=24)).isoformat()
    assert domain().evaluate_plan(draft(), source, NOW)["feasibility"] == "checks_passed"
    source["as_of"] = (NOW - timedelta(hours=24, microseconds=1)).isoformat()
    assert domain().evaluate_plan(draft(), source, NOW)["feasibility"] == "needs_data"
    source["as_of"] = (NOW + timedelta(microseconds=1)).isoformat()
    assert domain().evaluate_plan(draft(), source, NOW)["feasibility"] == "needs_data"


def test_evaluation_rejects_naive_clock_timezone():
    module = domain()
    with pytest.raises(module.DomainError):
        module.evaluate_plan(draft(), snapshot(), NOW.replace(tzinfo=None))


def test_past_activity_window_is_not_ready_for_current_confirmation():
    assert domain().evaluate_plan(draft(), snapshot(), NOW + timedelta(days=1))["feasibility"] == "infeasible"


def test_metrics_round_average_to_cent_with_half_up_and_ratio_precision_is_explicit():
    value = snapshot()
    value["orders"] = [order("O1", NOW.isoformat(), "10000"),
                       order("O2", NOW.isoformat(), "10001")]
    result = domain().calculate_metrics(value, NOW - timedelta(days=1), NOW + timedelta(days=1), NOW)
    assert result["average_order_minor"] == "10001"


def test_evaluation_is_deterministic_and_does_not_mutate_inputs():
    value, source = draft(), snapshot()
    original_value, original_source = deepcopy(value), deepcopy(source)
    result = domain().evaluate_plan(value, source, NOW)
    assert result == domain().evaluate_plan(value, source, NOW)
    assert value == original_value
    assert source == original_source


@pytest.mark.parametrize("location", ["order_item", "draft_line"])
def test_non_string_sku_reference_returns_domain_error_instead_of_type_error(location):
    module = domain()
    if location == "order_item":
        source = metrics_fixture()
        source["orders"][0]["items"][0]["sku_id"] = []
        with pytest.raises(module.DomainError):
            module.validate_snapshot(source)
    else:
        value = draft()
        value["lines"][0]["sku_id"] = []
        with pytest.raises(module.DomainError):
            module.evaluate_plan(value, snapshot(), NOW)


def test_large_integer_minor_amounts_are_exact_beyond_default_decimal_precision():
    source, value = snapshot(), draft()
    source["skus"][0].update(base_price_minor="9223372036854775807", available_qty=9223372036854775807)
    value["lines"][0]["quantities"] = {"low": 9223372036854775807,
                                      "base": 9223372036854775807,
                                      "high": 9223372036854775807}
    result = domain().evaluate_plan(value, source, NOW)
    assert candidate(result, "baseline")["scenarios"]["base"]["revenue_minor"] == (
        "85070591730234615847396907784232501249"
    )


def test_zero_effective_shift_does_not_divide_by_zero_or_promise_extra_people():
    value = draft()
    value["resources"][0]["efficiency"] = "0"
    result = domain().evaluate_plan(value, snapshot(), NOW)
    row = next(row for row in result["resources"] if row["role"] == "客服")
    assert row["available_hours"] == "0"
    assert row["additional_people"] is None
    assert result["feasibility"] == "infeasible"


def test_zero_payment_denominator_has_unknown_amount_refund_rate():
    source = snapshot()
    source["orders"] = [{"order_id": "free", "paid_at": NOW.isoformat(), "status": "paid",
                         "channel": "自然流量", "paid_minor": "0",
                         "items": [{"sku_id": "A", "qty": 1, "paid_minor": "0"}]}]
    result = domain().calculate_metrics(source, NOW - timedelta(days=1), NOW + timedelta(days=1), NOW)
    assert result["paid_order_count"] == 1
    assert result["average_order_minor"] == "0"
    assert result["cohort_refund_amount_rate"] is None


@pytest.mark.parametrize("start,end", [(NOW, NOW), (NOW, NOW - timedelta(seconds=1))])
def test_invalid_half_open_metric_window_is_rejected(start, end):
    module = domain()
    with pytest.raises(module.DomainError) as error:
        module.calculate_metrics(snapshot(), start, end, NOW)
    assert error.value.code == "invalid_window"


def test_direct_price_equal_to_original_is_not_a_publishable_discount():
    value = draft()
    value["lines"][0]["direct_price_minor"] = "10000"
    assert domain().evaluate_plan(value, snapshot(), NOW)["feasibility"] == "infeasible"


def test_utc_and_shanghai_equivalent_instants_produce_same_metrics():
    module = domain()
    source = metrics_fixture()
    start, end = datetime(2026, 11, 1, tzinfo=TZ), datetime(2026, 11, 11, tzinfo=TZ)
    expected = module.calculate_metrics(source, start, end, NOW)
    source["as_of"] = NOW.astimezone(timezone.utc).isoformat()
    source["orders"][1]["paid_at"] = "2026-10-31T16:00:00+00:00"
    assert module.calculate_metrics(source, start.astimezone(timezone.utc), end, NOW) == expected


def test_draft_and_now_accept_equivalent_utc_instants_without_changing_calculation():
    module = domain()
    value, source = draft(), snapshot()
    expected = module.evaluate_plan(value, source, NOW)
    value["start_at"] = "2026-11-10T16:00:00+00:00"
    value["tasks"][1]["due_at"] = "2026-11-10T16:00:00+00:00"
    actual = module.evaluate_plan(value, source, NOW.astimezone(timezone.utc))
    assert actual["candidates"] == expected["candidates"]
    assert actual["feasibility"] == expected["feasibility"] == "checks_passed"


def test_quantity_outside_persistence_integer_range_is_rejected():
    module = domain()
    value = draft()
    value["lines"][0]["quantities"]["high"] = 10 ** 100
    with pytest.raises(module.DomainError):
        module.evaluate_plan(value, snapshot(), NOW)
