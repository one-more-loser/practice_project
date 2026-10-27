from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from cs_agent.actions.service import ActionService
from cs_agent.commerce.checkout import CheckoutService
from cs_agent.commerce.expiration import expire_due
from cs_agent.commerce.fulfillment import FulfillmentService
from cs_agent.core.context import Role
from cs_agent.db.business_tables import (
    address_changes,
    inventory,
    order_items,
    orders,
    shipments,
    wallet_entries,
)
from cs_agent.wallet.service import WalletService


def test_database_rejects_order_with_missing_shipping_snapshot(business):
    db, _, ctx, now = business
    order_id = submit(
        db, ctx, make_confirmation(db, ctx, now, [{"sku_id": "sku_cup_ivory", "qty": 1}]), now
    )[0]["business_ref"]
    with db.transaction() as session:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.execute(
                    update(orders)
                    .where(orders.c.order_id == order_id)
                    .values(current_shipping_id="missing_snapshot")
                )
                session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_expiration_and_payment_at_deadline_never_charge_closed_order(business):
    db, _, ctx, now = business
    order_id = submit(
        db, ctx, make_confirmation(db, ctx, now, [{"sku_id": "sku_cup_ivory", "qty": 1}]), now
    )[0]["business_ref"]
    with db.transaction() as session:
        WalletService().topup(session, ctx, "10000", uuid4().hex, now)
        ref = ActionService().propose(
            session, ctx, "order.pay", order_id, {}, 1, now + timedelta(minutes=14)
        )["confirmation_id"]
    barrier = Barrier(2)

    def pay():
        barrier.wait(timeout=10)
        return submit(db, ctx, ref, now + timedelta(minutes=15))[1]

    def close():
        barrier.wait(timeout=10)
        with db.transaction() as session:
            return expire_due(session, now + timedelta(minutes=15))

    with ThreadPoolExecutor(2) as pool:
        payment, expiration = pool.submit(pay), pool.submit(close)
        assert payment.result(timeout=20) == 409
        assert expiration.result(timeout=20) == 1
    with db.transaction() as session:
        assert WalletService().read(session, ctx)["balance_minor"] == "10000"
        row = session.execute(select(orders).where(orders.c.order_id == order_id)).mappings().one()
        assert row["trade_status"] == "cancelled_unpaid" and row["paid_minor"] == 0
        assert (
            session.scalar(
                select(inventory.c.reserved_qty).where(inventory.c.sku_id == "sku_cup_ivory")
            )
            == 0
        )


def make_confirmation(db, ctx, now, items):
    with db.transaction() as session:
        quote, status = CheckoutService().quote(
            session,
            ctx,
            items,
            "shipping_sim_a" if ctx.subject_id == "customer_a" else "shipping_sim_b",
            uuid4().hex,
            now,
        )
        assert status == 200
        return ActionService().propose(session, ctx, "order.create", quote["quote_id"], {}, 1, now)[
            "confirmation_id"
        ]


def submit(db, ctx, ref, now):
    with db.transaction() as session:
        return ActionService().submit(session, ctx, ref, "confirm", uuid4().hex, now)


def test_two_connections_compete_for_last_piece(business):
    db, _, ctx, now = business
    other = replace(ctx, subject_id="customer_b")
    refs = [
        make_confirmation(db, who, now, [{"sku_id": "sku_vase", "qty": 1}]) for who in (ctx, other)
    ]
    barrier = Barrier(2)

    def run(who, ref):
        barrier.wait(timeout=10)
        return submit(db, who, ref, now)[1]

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, who, ref) for who, ref in zip((ctx, other), refs)]
        assert sorted(f.result(timeout=20) for f in futures) == [200, 409]
    with db.transaction() as session:
        row = (
            session.execute(select(inventory).where(inventory.c.sku_id == "sku_vase"))
            .mappings()
            .one()
        )
        assert (row["stock_qty"], row["reserved_qty"]) == (1, 1)
        assert session.scalar(select(func.count()).select_from(orders)) == 1


def test_distinct_payment_confirmations_never_double_debit(business):
    db, _, ctx, now = business
    create = make_confirmation(db, ctx, now, [{"sku_id": "sku_cup_ivory", "qty": 1}])
    order_id = submit(db, ctx, create, now)[0]["business_ref"]
    with db.transaction() as session:
        WalletService().topup(session, ctx, "10000", uuid4().hex, now)
        refs = [
            ActionService().propose(session, ctx, "order.pay", order_id, {}, 1, now)[
                "confirmation_id"
            ]
            for _ in range(2)
        ]
    barrier = Barrier(2)

    def run(ref):
        barrier.wait(timeout=10)
        return submit(db, ctx, ref, now)[1]

    with ThreadPoolExecutor(2) as pool:
        assert sorted(f.result(timeout=20) for f in [pool.submit(run, ref) for ref in refs]) == [
            200,
            409,
        ]
    with db.transaction() as session:
        assert WalletService().read(session, ctx)["balance_minor"] == "6000"
        assert (
            session.scalar(
                select(func.count())
                .select_from(wallet_entries)
                .where(wallet_entries.c.kind == "payment")
            )
            == 1
        )


def test_address_and_shipping_serialize_and_shipping_snapshot_is_consistent(business):
    db, _, ctx, now = business
    order_id = submit(
        db, ctx, make_confirmation(db, ctx, now, [{"sku_id": "sku_cup_ivory", "qty": 1}]), now
    )[0]["business_ref"]
    with db.transaction() as session:
        WalletService().topup(session, ctx, "10000", uuid4().hex, now)
        pay = ActionService().propose(session, ctx, "order.pay", order_id, {}, 1, now)[
            "confirmation_id"
        ]
    assert submit(db, ctx, pay, now)[1] == 200
    with db.transaction() as session:
        private = CheckoutService().create_shipping_input(
            session,
            ctx,
            {
                "recipient": "模拟收件人",
                "phone": "13900007777",
                "province": "浙江省",
                "city": "杭州市",
                "district": "西湖区",
                "street": "测试路88号",
                "purpose": "address_change",
            },
            uuid4().hex,
            now,
        )[0]
        ref = ActionService().propose(
            session,
            ctx,
            "order.address.change",
            order_id,
            {"shipping_input_ref": private["business_ref"]},
            2,
            now,
        )["confirmation_id"]
        quantities = {
            row.order_item_id: row.ordered_qty
            for row in session.execute(
                select(order_items).where(order_items.c.order_id == order_id)
            )
        }
    shop = replace(ctx, subject_id="shop_admin", role=Role.SHOP_ADMIN)
    barrier = Barrier(2)

    def ship():
        barrier.wait(timeout=10)
        with db.transaction() as session:
            return FulfillmentService().ship(
                session, shop, order_id, quantities, 2, uuid4().hex, now
            )[1]

    def change():
        barrier.wait(timeout=10)
        return submit(db, ctx, ref, now)[1]

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(ship), pool.submit(change)
        results = [a.result(timeout=20), b.result(timeout=20)]
    assert sorted(results) == [200, 409]
    # 若改址获胜，再以新版本发货；包裹必须绑定订单最终选择的快照。
    if results[1] == 200:
        with db.transaction() as session:
            assert (
                FulfillmentService().ship(session, shop, order_id, quantities, 3, uuid4().hex, now)[
                    1
                ]
                == 200
            )
    with db.transaction() as session:
        row = session.execute(select(orders).where(orders.c.order_id == order_id)).mappings().one()
        shipped = (
            session.execute(select(shipments).where(shipments.c.order_id == order_id))
            .mappings()
            .one()
        )
        assert row["current_shipping_id"] == shipped["shipping_snapshot_id"]
        assert row["change_count"] == (1 if results[1] == 200 else 0)
        assert (
            session.scalar(select(func.count()).select_from(address_changes)) == row["change_count"]
        )
