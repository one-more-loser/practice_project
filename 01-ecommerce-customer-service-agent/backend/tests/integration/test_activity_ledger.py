"""固定test库独占验证。仅父任务协调后运行，business fixture会reset固定test。"""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from cs_agent.actions.repository import provenance
from cs_agent.activities import pricing
from cs_agent.activities.contracts import VERSION, manifest_digest
from cs_agent.activities.outbox import claim, finish
from cs_agent.activities.repository import receipt
from cs_agent.activities.service import internal_result
from cs_agent.core.errors import AppError
from cs_agent.db.activity_tables import activities, allocations, deliveries, lines, slots
from cs_agent.db.business_tables import inventory, skus


def install(db, ctx, now):
    m = dict(
        contract_version=VERSION,
        store_id=ctx.store_id,
        plan_id="gold_plan",
        plan_version=1,
        rule="direct",
        currency="CNY",
        start_at=(now - timedelta(hours=1)).isoformat(),
        end_at=(now + timedelta(hours=1)).isoformat(),
        scenario_policy="base",
        budget_cap_minor="100000",
        fixed_fee_minor="10000",
        variable_budget_cap_minor="90000",
        lines=[
            dict(
                sku_id="sku_cup_ivory",
                base_price_minor="10000",
                activity_price_minor="9200",
                quantity_cap=100,
                price_version=1,
            )
        ],
        source_snapshot_id="gold_source",
        source_snapshot_revision=1,
        source_kind="synthetic",
    )
    with db.transaction() as s:
        s.execute(update(skus).where(skus.c.sku_id == "sku_cup_ivory").values(price_minor=10000))
        s.execute(
            update(inventory).where(inventory.c.sku_id == "sku_cup_ivory").values(stock_qty=100)
        )
        values = dict(
            intent_id="gold_intent",
            plan_id="gold_plan",
            plan_version=1,
            approval_ref="gold_approval",
            manifest_digest=manifest_digest(m),
            manifest=m,
            actor_ref=ctx.subject_id,
            actor_role=ctx.role.value,
            session_ref=ctx.session_ref,
            auth_version=1,
            capability_revision=1,
            preview_ref="gold_preview",
            grant_ref="gold_grant",
            grant_expires_at=now + timedelta(seconds=60),
            status="committed",
            activity_id="gold_activity",
            **provenance(ctx, now),
        )
        s.execute(slots.insert().values(**values))
        s.execute(
            activities.insert().values(
                activity_id="gold_activity",
                intent_id="gold_intent",
                manifest=m,
                phase="active",
                revision=1,
                start_at=now - timedelta(hours=1),
                end_at=now + timedelta(hours=1),
                variable_cap_minor=90000,
                reserved_discount_minor=0,
                consumed_discount_minor=0,
                actual_revision=0,
                snapshot_id="gold_actual",
                watermark=now,
                **provenance(ctx, now),
            )
        )
        s.execute(
            lines.insert().values(
                line_id="gold_line",
                activity_id="gold_activity",
                sku_id="sku_cup_ivory",
                base_price_minor=10000,
                effective_price_minor=9200,
                price_version=1,
                quantity_cap=100,
                reserved_qty=0,
                consumed_qty=0,
                **provenance(ctx, now),
            )
        )
        receipt(
            s,
            s.execute(select(slots).where(slots.c.intent_id == values["intent_id"]))
            .mappings()
            .one(),
            now,
            dict(activity_id="gold_activity", revision=1, phase="active"),
            enqueue_event=True,
        )


def order_line(qty=2):
    return dict(
        sku_id="sku_cup_ivory",
        qty=qty,
        base_price_minor="10000",
        effective_price_minor="9200",
        activity_id="gold_activity",
        activity_revision=1,
    )


def test_gold_local_ledger_refund_replay_and_delivery_lease(business):
    db, _, ctx, now = business
    install(db, ctx, now)
    with db.transaction() as s:
        sku = s.execute(select(skus).where(skus.c.sku_id == "sku_cup_ivory")).mappings().one()
        assert pricing.resolve_price(s, ctx.store_id, sku, now)["effective_price_minor"] == 9200
        pricing.reserve_order(s, ctx, "gold_order", [order_line()], now)
    with db.transaction() as s:
        a = s.execute(select(activities)).mappings().one()
        assert (a["reserved_discount_minor"], a["consumed_discount_minor"]) == (1600, 0)
        pricing.consume_order(s, ctx, "gold_order", now)
        pricing.consume_order(s, ctx, "gold_order", now)
        pricing.record_refund(
            s,
            ctx,
            "refund_gold",
            "gold_order",
            "9200",
            now,
            allocations=[{"sku_id": "sku_cup_ivory", "amount_minor": "9200"}],
        )
        pricing.record_refund(
            s,
            ctx,
            "refund_gold",
            "gold_order",
            "9200",
            now,
            allocations=[{"sku_id": "sku_cup_ivory", "amount_minor": "9200"}],
        )
        pricing.release_order(s, ctx, "gold_order", now)
    with db.transaction() as s:
        a = s.execute(select(activities)).mappings().one()
        r = s.execute(select(allocations)).mappings().one()
        assert (a["reserved_discount_minor"], a["consumed_discount_minor"]) == (0, 1600)
        assert (r["payment_minor"], r["refund_minor"], r["qty"]) == (18400, 9200, 2)
        actual = (
            s.execute(
                select(deliveries)
                .where(deliveries.c.kind == "actuals")
                .order_by(deliveries.c.sequence.desc())
            )
            .mappings()
            .first()["payload"]
        )
        assert (actual["payment_minor"], actual["refund_minor"], actual["paid_units"]) == (
            "18400",
            "9200",
            2,
        )
        first = claim(s, now, (ctx.store_id,))
        assert first["kind"] == "receipt"
    with db.transaction() as s:
        assert claim(s, now, (ctx.store_id,)) is None
        recovered = claim(s, now + timedelta(seconds=16), (ctx.store_id,))
        assert (
            recovered["event_id"] == first["event_id"]
            and recovered["lease_token"] != first["lease_token"]
        )
        assert not finish(s, first, now + timedelta(seconds=16), True, "stale")
        assert finish(s, recovered, now + timedelta(seconds=16), True, "consumer_ack")


def test_release_idempotent_quota_failure_and_expiry_barrier(business):
    db, _, ctx, now = business
    install(db, ctx, now)
    with db.transaction() as s:
        pricing.reserve_order(s, ctx, "cancel_order", [order_line()], now)
        pricing.release_order(s, ctx, "cancel_order", now)
        pricing.release_order(s, ctx, "cancel_order", now)
        assert s.execute(select(lines.c.reserved_qty)).scalar_one() == 0
    with pytest.raises(AppError) as exc:
        with db.transaction() as s:
            pricing.reserve_order(s, ctx, "too_large", [order_line(101)], now)
    assert exc.value.code == "activity_requote_required"
    with db.transaction() as s:
        # 这是另一个尚未提交的slot，模拟grant响应丢失；到期闭合与迟到提交共用行锁。
        old = s.execute(select(slots)).mappings().one()
        s.execute(
            slots.insert().values(
                **dict(
                    old,
                    intent_id="pending",
                    approval_ref="approval2",
                    plan_id="plan2",
                    status="pending",
                    activity_id=None,
                    receipt=None,
                    grant_expires_at=now - timedelta(seconds=1),
                )
            )
        )
        closed = internal_result(s, ctx.store_id, "pending", now, "finalize-expired")
        assert closed["status"] == "closed_no_effect" and closed["activity_id"] is None
    with db.transaction() as s:
        assert internal_result(s, ctx.store_id, "pending", now)["status"] == "closed_no_effect"
        assert internal_result(s, ctx.store_id, "absent", now) == {"status": "unknown"}
