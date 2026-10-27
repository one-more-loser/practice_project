"""Explicit synthetic opening fixture in the guarded fresh run DB; no legacy seed/reset."""
import hashlib
import json
import secrets
from collections import defaultdict
from datetime import datetime, timedelta

from int20261026_common import INITIAL_TIME, OUT, PRIVATE, RUN, apply_cs, write

BATCH = "int_20261026_01"
STORES = ("int_store_a", "int_store_b")
ACCOUNTS = {
    "int_admin_a": (STORES[0], "shop_admin"), "int_ops_a": (STORES[0], "supervisor"),
    "int_viewer_a": (STORES[0], "support"), "int_revocable_a": (STORES[0], "supervisor"),
    "int_admin_b": (STORES[1], "shop_admin"), "int_customer_a": (STORES[0], "customer"),
    "int_customer_b": (STORES[1], "customer"),
}
SKU_ROWS = [("int_sku_a1", STORES[0], 1000, 400), ("int_sku_a2", STORES[0], 2000, 800),
            ("int_sku_b1", STORES[1], 1200, 500)]


def seed():
    from cs_agent.config import Settings
    from cs_agent.core.context import Role
    from cs_agent.db import business_tables as t
    from cs_agent.db.models import RoleGrant, ShippingInput, SourceBatch, Store, Subject
    from cs_agent.db.seed_business import seed_accounts
    from cs_agent.db.session import verify_target
    from cs_agent.identity.operations_seed import seed_operations_capabilities
    from cs_agent.identity.security import hash_password
    from int20261026_prepare import engine_for
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session
    apply_cs("cs_owner")
    engine = engine_for("cs_owner")
    verify_target(engine, Settings(), migration=True)
    now = datetime.fromisoformat(INITIAL_TIME)
    opening = now - timedelta(days=10)
    passwords = {name: secrets.token_urlsafe(24) for name in ACCOUNTS}
    with Session(engine) as session, session.begin():
        if session.scalar(select(SourceBatch.batch_id).limit(1)):
            raise RuntimeError("nonempty run batch; no automatic reseed/update")
        digest = hashlib.sha256(json.dumps([RUN, ACCOUNTS, SKU_ROWS], sort_keys=True).encode()).hexdigest()
        session.add(SourceBatch(batch_id=BATCH, generation=1, source_kind="synthetic", seed=20261026,
                                fingerprint=digest, manifest_revision=RUN, status="active", created_at=opening))
        session.flush()

        def common(store, at=opening):
            return {"store_id": store, "source_kind": "synthetic", "data_batch_id": BATCH,
                    "batch_generation": 1, "created_at": at, "updated_at": at, "version": 1}

        for store in STORES:
            fields = common(store)
            fields.pop("store_id")
            session.add(Store(store_id=store, name="INT合成店" + store[-1].upper(), status="active",
                              currency="CNY", business_timezone="Asia/Shanghai", **fields))
        session.flush()
        for name, (store, role) in ACCOUNTS.items():
            customer = role == "customer"
            session.add(Subject(subject_id=name, customer_id="c_" + name if customer else None,
                subject_type="customer" if customer else "staff", login_name=name,
                password_digest=hash_password(passwords[name]), account_status="active", auth_version=1, **common(store)))
        session.flush()
        for name, (store, role) in ACCOUNTS.items():
            if role != "customer":
                session.add(RoleGrant(grant_id="grant_" + name, staff_id=name, role=role, grant_status="active",
                                      valid_from=opening, valid_to=now + timedelta(days=7), **common(store)))
            else:
                session.add(ShippingInput(input_id="ship_" + name, owner_id=name, recipient="合成收件人",
                    phone="13800123456", province="广东省", city="深圳市", district="南山区",
                    street="合成测试路88号", postal_code="518000", purpose="checkout", **common(store)))
        session.flush()
        seed_accounts(session)
        all_caps = ["metrics.read", "cost.read", "data.import", "plan.edit", "plan.confirm", "activity.publish", "activity.manage"]
        for store in STORES:
            assignments = [(name, Role(role), cap) for name, (s, role) in ACCOUNTS.items() if s == store
                for cap in (all_caps if "admin" in name or "revocable" in name else
                            ["metrics.read", "cost.read", "data.import", "plan.edit"] if "ops" in name else
                            ["metrics.read"] if "viewer" in name else [])]
            seed_operations_capabilities(session, assignments=assignments, store_id=store, data_batch_id=BATCH,
                batch_generation=1, integration_run_id=RUN, now=opening, valid_to=now + timedelta(days=7))
        prices = {}
        stocks = {}
        for sku, store, price, cost in SKU_ROWS:
            prices[sku] = price
            stocks[sku] = [1000, 0]
            session.execute(t.products.insert().values(product_id="p_" + sku, name="合成商品" + sku,
                category="生活用品", description="INT隔离联调合成商品", aliases=[], image_slug="placeholder",
                status="active", source_revision=RUN, source_fingerprint=digest, **common(store)))
            session.execute(t.skus.insert().values(sku_id=sku, product_id="p_" + sku, specifications={"规格": "合成"},
                price_minor=price, currency="CNY", cost_minor=cost, price_version=1, status="active", **common(store)))
            session.execute(t.inventory.insert().values(sku_id=sku, stock_qty=1000, reserved_qty=0, **common(store)))
            session.execute(t.stock_movements.insert().values(movement_id="opening_" + sku, sku_id=sku, operation_id=None,
                business_ref=sku, kind="opening", stock_delta=1000, reserved_delta=0, stock_before=0, stock_after=1000,
                reserved_before=0, reserved_after=0, reason="synthetic_run_opening", occurred_at=opening, recorded_at=opening, **common(store)))
        balances, sequences = {}, {}

        def operation(identity, owner, store, action, at, amount):
            session.execute(t.operations.insert().values(operation_id=identity, actor_id=owner, actor_role="customer",
                action=action, idempotency_key=identity, parameters_digest=hashlib.sha256(identity.encode()).hexdigest(),
                resource_ref=identity, confirmation_id=None, status="succeeded", result={"synthetic": True, "amount_minor": str(amount)},
                http_status=200, **common(store, at)))

        for owner, (store, role) in ACCOUNTS.items():
            if role != "customer":
                continue
            op = "opening_topup_" + owner
            operation(op, owner, store, "wallet.topup", opening, 1000000)
            session.execute(t.wallet_entries.insert().values(entry_id=op, account_id="wal_" + owner, owner_id=owner,
                operation_id=op, sequence=1, kind="topup", currency="CNY", amount_minor=1000000,
                balance_before_minor=0, balance_after_minor=1000000, business_ref=owner,
                occurred_at=opening, recorded_at=opening, **common(store)))
            balances[owner], sequences[owner] = 1000000, 1

        recipes = [
            ("int_order_start", STORES[0], now - timedelta(days=1), "paid", [("int_sku_a1", 2)]),
            ("int_order_repeat", STORES[0], now - timedelta(hours=1), "paid", [("int_sku_a1", 1), ("int_sku_a1", 2), ("int_sku_a2", 1)]),
            ("int_order_end", STORES[0], now, "paid", [("int_sku_a2", 1)]),
            ("int_order_after", STORES[0], now + timedelta(seconds=1), "paid", [("int_sku_a1", 1)]),
            ("int_order_unpaid", STORES[0], None, "awaiting_payment", [("int_sku_a1", 2)]),
            ("int_order_zero", STORES[0], now - timedelta(minutes=30), "paid", [("int_sku_a1", 1)]),
            ("int_order_b", STORES[1], now - timedelta(hours=1), "paid", [("int_sku_b1", 1)]),
        ]
        dictionary = []
        from cs_agent.commerce.checkout import POLICY
        for identity, store, paid_at, status, lines in recipes:
            owner = "int_customer_" + store[-1]
            created = (min(paid_at, now) if paid_at else now - timedelta(minutes=5)) - timedelta(minutes=1)
            total = sum(prices[s] * qty for s, qty in lines)
            zero = identity == "int_order_zero"
            allocated = 0 if zero else total
            paid = allocated if status == "paid" else 0
            session.execute(t.quotes.insert().values(quote_id="quote_" + identity, owner_id=owner,
                shipping_input_id="ship_" + owner, lines=[], cart_bindings=[], currency="CNY", goods_minor=total,
                discount_minor=total - allocated, freight_minor=0, payable_minor=allocated, policy_snapshot=POLICY,
                expires_at=created + timedelta(minutes=5), status="consumed", **common(store, created)))
            session.execute(t.orders.insert().values(order_id=identity, owner_id=owner, display_no=identity.upper(),
                quote_id="quote_" + identity, trade_status=status, fulfillment_status="unshipped", currency="CNY",
                goods_minor=total, discount_minor=total - allocated, freight_minor=0, payable_minor=allocated,
                paid_minor=paid, policy_snapshot=POLICY, current_shipping_id="ss_" + identity,
                payment_expires_at=created + timedelta(minutes=15), paid_at=paid_at, **common(store, created)))
            session.execute(t.shipping_snapshots.insert().values(snapshot_id="ss_" + identity, order_id=identity,
                owner_id=owner, input_id="ship_" + owner, recipient="合成收件人", phone="13800123456",
                province="广东省", city="深圳市", district="南山区", street="合成测试路88号", postal_code="518000", **common(store, created)))
            by_sku = defaultdict(int)
            for index, (sku, qty) in enumerate(lines):
                amount = 0 if zero else prices[sku] * qty
                base, tail = divmod(amount, qty)
                session.execute(t.order_items.insert().values(order_item_id=identity + "_" + str(index), order_id=identity,
                    sku_id=sku, product_name="合成商品" + sku, specifications={"规格": "合成"}, image_slug="placeholder",
                    unit_price_minor=prices[sku], ordered_qty=qty, goods_minor=prices[sku] * qty,
                    discount_minor=prices[sku] * qty - amount, allocated_minor=amount,
                    paid_goods_minor=amount if status == "paid" else 0,
                    unit_allocations={"base_minor": str(base), "tail_minor": str(base + tail), "quantity": qty},
                    allocation_revision="ALLOC-LAST-UNIT-v1", **common(store, created)))
                by_sku[sku] += qty
            op = "pay_" + identity if paid else None
            for sku, qty in by_sku.items():
                before, reserved = stocks[sku]
                after, held = (before - qty, reserved) if status == "paid" else (before, reserved + qty)
                stocks[sku] = [after, held]
                session.execute(t.reservations.insert().values(reservation_id="r_" + identity + "_" + sku,
                    order_id=identity, sku_id=sku, qty=qty, status="consumed" if status == "paid" else "reserved",
                    expires_at=created + timedelta(minutes=15), **common(store, created)))
                session.execute(t.stock_movements.insert().values(movement_id="stock_" + identity + "_" + sku,
                    sku_id=sku, operation_id=None, business_ref=identity, kind="payment" if status == "paid" else "reservation",
                    stock_delta=after - before, reserved_delta=held - reserved, stock_before=before, stock_after=after,
                    reserved_before=reserved, reserved_after=held, reason="synthetic_opening_fixture",
                    occurred_at=paid_at or created, recorded_at=paid_at or created, **common(store, created)))
            if paid:
                operation(op, owner, store, "order.pay", paid_at, paid)
                balance = balances[owner]
                balances[owner] -= paid
                sequences[owner] += 1
                session.execute(t.wallet_entries.insert().values(entry_id=op, account_id="wal_" + owner, owner_id=owner,
                    operation_id=op, sequence=sequences[owner], kind="payment", currency="CNY", amount_minor=paid,
                    balance_before_minor=balance, balance_after_minor=balances[owner], business_ref=identity,
                    occurred_at=paid_at, recorded_at=paid_at, **common(store, paid_at)))
                session.execute(t.payments.insert().values(payment_id=op, order_id=identity, operation_id=op, entry_id=op,
                    amount_minor=paid, currency="CNY", status="succeeded", paid_at=paid_at, **common(store, paid_at)))
            dictionary.append({"order_id": identity, "store_id": store, "owner_id": owner, "created_at": created,
                "paid_at": paid_at, "trade_status": status, "paid_minor": paid, "freight_minor": 0,
                "items": [{"sku_id": sku, "qty": qty} for sku, qty in lines],
                "snapshot_at_initial": "excluded_future_payment" if paid_at and paid_at > now else "included",
                "projected_status": "paid" if paid else "unpaid"})
        for sku, (stock, reserved) in stocks.items():
            session.execute(t.inventory.update().where(t.inventory.c.sku_id == sku).values(stock_qty=stock, reserved_qty=reserved))
        for owner, balance in balances.items():
            session.execute(t.wallets.update().where(t.wallets.c.owner_id == owner).values(balance_minor=balance, ledger_sequence=sequences[owner]))
        # Opening-fixture consistency, not replay of the real checkout/refund lifecycle.
        assert session.execute(text("SELECT count(*) FROM customer_order o WHERE paid_minor<>(SELECT sum(paid_goods_minor) FROM order_item i WHERE i.order_id=o.order_id)")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM customer_order o WHERE paid_minor>0 AND paid_minor<>(SELECT sum(amount_minor) FROM order_payment p WHERE p.order_id=o.order_id)")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM inventory i WHERE stock_qty<>(SELECT sum(stock_delta) FROM stock_movement m WHERE m.sku_id=i.sku_id) OR reserved_qty<>(SELECT sum(reserved_delta) FROM stock_movement m WHERE m.sku_id=i.sku_id)")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM wallet_account w WHERE balance_minor<>(SELECT balance_after_minor FROM wallet_entry e WHERE e.account_id=w.account_id ORDER BY sequence DESC LIMIT 1)")).scalar_one() == 0
    write(PRIVATE / "accounts-private.json", passwords)
    inputs = {"schema_version": "OPS-SYNTHETIC-INPUTS-v1", "source_kind": "synthetic", "inputs_version": RUN + "-v1",
        "store_id": STORES[0], "data_batch_id": BATCH, "batch_generation": 1,
        "skus": {sku: {"cost_minor": cost, "variable_fee_minor": 50} for sku, store, price, cost in SKU_ROWS if store == STORES[0]},
        "capacity_basis": "synthetic assumptions: picker 2 persons x 8h x 30 units/h; support 1 person x 8h x 20 orders/h; not measured"}
    write(PRIVATE / "synthetic-inputs.json", inputs)
    write(OUT / "sample-dictionary.json", {"integration_run_id": RUN, "batch": BATCH, "generation": 1,
        "reference_now": now, "accounts": {k: {"store_id": s, "role": r} for k, (s, r) in ACCOUNTS.items()},
        "orders": dictionary, "skus": SKU_ROWS, "opening_stock_per_sku": 1000,
        "synthetic_capacity": {"picker_persons": 2, "picker_hours": 8, "picker_units_per_hour": 30,
                               "support_persons": 1, "support_hours": 8, "support_orders_per_hour": 20},
        "checks": ["order lines paid sum", "positive payments sum", "inventory movement sum", "wallet tail balance"],
        "fixture_limitations": ["opening snapshots seeded directly; not business workflow acceptance",
            "future payment retained to exercise source as_of cutoff; current inventory is not a historical snapshot",
            "zero paid order uses full synthetic discount and projects unpaid; no new business rule",
            "no refunds/activity/Outbox scenario executed in this stage"],
        "expected_window": {"start_at": (now - timedelta(days=1)), "end_at": now,
                            "paid_order_ids": ["int_order_start", "int_order_repeat"],
                            "paid_orders": 2, "paid_minor": "7000", "units": 6}})
    engine.dispose()
    print(json.dumps({"seed": "passed", "batch": BATCH, "orders": len(dictionary), "stores": len(STORES), "accounts": len(ACCOUNTS)}))
