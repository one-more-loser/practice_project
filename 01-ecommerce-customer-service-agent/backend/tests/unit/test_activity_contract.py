from datetime import UTC, datetime, timedelta

import pytest

from cs_agent.activities.contracts import manifest_digest, validate_grant, validate_manifest
from cs_agent.core.errors import AppError


def manifest():
    return dict(
        contract_version="XOPS-ACTIVITY-v0.1",
        store_id="store_sim",
        plan_id="p",
        plan_version=1,
        rule="direct",
        currency="CNY",
        start_at="2026-10-23T00:00:00Z",
        end_at="2026-10-24T00:00:00Z",
        scenario_policy="base",
        budget_cap_minor="100000",
        fixed_fee_minor="10000",
        variable_budget_cap_minor="90000",
        lines=[
            dict(
                sku_id="s",
                base_price_minor="10000",
                activity_price_minor="9200",
                quantity_cap=100,
                price_version=1,
            )
        ],
        source_snapshot_id="snap",
        source_snapshot_revision=1,
        source_kind="synthetic",
    )


def test_manifest_keeps_original_time_strings():
    a = manifest()
    b = dict(a, start_at="2026-10-23T08:00:00+08:00")
    assert manifest_digest(a) != manifest_digest(b)
    assert validate_manifest(a) is a


@pytest.mark.parametrize(
    "change",
    [
        {"variable_budget_cap_minor": "89999"},
        {"budget_cap_minor": 100000},
        {"start_at": "2026-10-23T00:00:00"},
        {"extra": True},
        {"plan_version": True},
        {"scenario_policy": []},
        {"source_kind": {}},
        {"budget_cap_minor": "9223372036854775808"},
    ],
)
def test_manifest_rejects_unsafe_contract(change):
    with pytest.raises(AppError):
        validate_manifest(dict(manifest(), **change))


def test_grant_rejects_expired_and_malformed():
    now = datetime(2026, 10, 23, tzinfo=UTC)
    for value in (None, [], {}, {"expires_at": "2026-10-22T00:00:00Z"}):
        with pytest.raises(AppError):
            validate_grant(value, {}, manifest(), now)


def test_grant_timing_binding_and_bool_revision():
    m = manifest()
    now = datetime(2026, 10, 23, tzinfo=UTC)
    expected = dict(
        store_id=m["store_id"],
        plan_id=m["plan_id"],
        approval_ref="approval",
        intent_id="intent",
        manifest_digest=manifest_digest(m),
        actor_ref="actor",
        auth_version=1,
        capability_revision=1,
        preview_ref="preview",
    )
    grant = dict(
        expected,
        contract_version="XOPS-ACTIVITY-v0.1",
        grant_ref="grant",
        plan_version=1,
        manifest=m,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=60)).isoformat(),
    )
    assert validate_grant(grant, expected, m, now) is grant
    for bad in (
        dict(grant, actor_ref="other"),
        dict(grant, auth_version=True),
        dict(grant, plan_version=True),
    ):
        with pytest.raises(AppError):
            validate_grant(bad, expected, m, now)
    with pytest.raises(AppError) as error:
        validate_grant(grant, expected, m, now + timedelta(seconds=60))
    assert error.value.code == "activity_grant_expired"


def test_reservation_rechecks_trusted_clock_after_activity_lock(monkeypatch):
    from types import SimpleNamespace

    from cs_agent.activities import pricing

    now = datetime(2026, 10, 23, tzinfo=UTC)

    class Empty:
        def mappings(self):
            return self

        def first(self):
            return None

    class Session:
        info = {"clock": SimpleNamespace(now=lambda: now + timedelta(seconds=2))}

        def execute(self, stmt):
            return Empty()

    monkeypatch.setattr(
        pricing,
        "_lock_activities",
        lambda *args: {
            "a": dict(
                phase="active",
                revision=1,
                start_at=now - timedelta(hours=1),
                end_at=now + timedelta(seconds=1),
            )
        },
    )
    line = dict(
        sku_id="s",
        qty=1,
        base_price_minor="10000",
        effective_price_minor="9200",
        activity_id="a",
        activity_revision=1,
    )
    with pytest.raises(AppError) as error:
        pricing.reserve_order(
            Session(),
            SimpleNamespace(store_id="store", data_batch_id="batch", batch_generation=1),
            "order",
            [line],
            now,
        )
    assert error.value.code == "activity_requote_required"
