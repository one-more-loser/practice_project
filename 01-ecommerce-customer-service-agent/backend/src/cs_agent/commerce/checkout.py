from datetime import timedelta

from sqlalchemy import delete, select, update

from cs_agent.actions.repository import execute_once, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.catalog.service import sku_row
from cs_agent.commerce.pricing import line_amounts, quote_amounts
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.business_tables import (
    cart_items,
    carts,
    order_items,
    orders,
    payments,
    quotes,
    reservations,
    shipping_snapshots,
)
from cs_agent.db.models import ShippingInput
from cs_agent.identity.policies import require_role
from cs_agent.inventory.service import move_stock
from cs_agent.privacy.projections import PrivateContact, support_contact
from cs_agent.wallet.service import event, post_entry

POLICY = {
    "revision": "BUSINESS-REQ-CS-v0.2",
    "shipping_revision": "POL-SIM-SHIPPING-v1",
    "aftersale_revision": "POL-SIM-AFTERSALE-v1",
    "ordinary_return_hours": 168,
    "ordinary_original_freight_refund_minor": "0",
    "quality_original_freight_refund_minor": "0",
    "wrong_item_original_freight_refund_minor": "0",
    "transport_original_freight_refund_minor": "0",
    "quality_return_freight_responsibility": "shop",
    "wrong_item_return_freight_responsibility": "shop",
    "freight_minor": "0",
    "discount_minor": "0",
    "source": "approved_synthetic_business_rules",
    "currency": "CNY",
}


def shipping_input_row(session, ctx, ref, purpose=None):
    row = session.scalar(
        select(ShippingInput).where(
            ShippingInput.input_id == ref,
            ShippingInput.owner_id == ctx.subject_id,
            ShippingInput.store_id == ctx.store_id,
            ShippingInput.data_batch_id == ctx.data_batch_id,
            ShippingInput.batch_generation == ctx.batch_generation,
        )
    )
    if row is None:
        raise not_found()
    if purpose and row.purpose != purpose:
        raise AppError("invalid_input", 422, "请使用对应用途的本人收货资料")
    return row


def contact_from(row, ref=None):
    return PrivateContact(
        ref or row.input_id,
        row.recipient,
        row.phone,
        row.province,
        row.city,
        row.district,
        row.street,
        row.postal_code,
    )


def owned_order(session, ctx, order_id, *, lock=False, shop=False):
    require_role(ctx, Role.SHOP_ADMIN if shop else Role.CUSTOMER)
    stmt = select(orders).where(
        orders.c.order_id == order_id,
        orders.c.store_id == ctx.store_id,
        orders.c.data_batch_id == ctx.data_batch_id,
        orders.c.batch_generation == ctx.batch_generation,
    )
    if not shop:
        stmt = stmt.where(orders.c.owner_id == ctx.subject_id)
    row = session.execute(stmt.with_for_update() if lock else stmt).mappings().first()
    if row is None:
        raise not_found()
    return row


def check_version(row, expected_version):
    if row["version"] != expected_version:
        raise AppError("version_conflict", 409, "内容已更新，请刷新并重新确认")


def ensure_salable(row, qty):
    if type(qty) is not int or not 0 < qty <= 2147483647:
        raise AppError("invalid_input", 422, "商品数量须为合法正整数")
    if row["status"] != "active" or row["product_status"] != "active":
        raise AppError("state_conflict", 409, "商品已下架，请重新选择")
    if row["stock_qty"] - row["reserved_qty"] < qty:
        raise AppError("insufficient_stock", 409, "商品库存不足，请调整数量")


def quote_row(session, ctx, quote_id, lock=False):
    stmt = select(quotes).where(
        quotes.c.quote_id == quote_id,
        quotes.c.owner_id == ctx.subject_id,
        quotes.c.store_id == ctx.store_id,
        quotes.c.data_batch_id == ctx.data_batch_id,
        quotes.c.batch_generation == ctx.batch_generation,
    )
    row = session.execute(stmt.with_for_update() if lock else stmt).mappings().first()
    if row is None:
        raise not_found()
    return row


def validate_quote(session, ctx, row, now, *, lock=False):
    from cs_agent.db.business_tables import products, skus

    if row["status"] != "active" or row["expires_at"] <= now:
        raise AppError("quote_expired", 409, "报价已失效，请重新结算")
    if row["cart_bindings"]:
        cart = (
            session.execute(
                select(carts)
                .where(carts.c.cart_id == row["cart_bindings"][0]["cart_id"])
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if cart["version"] != row["cart_bindings"][0]["cart_version"]:
            raise AppError("quote_stale", 409, "购物车已变化，请重新结算")
    if lock:
        # 先按商品ID锁商品，再按SKU锁库存；同商品多规格不产生交叉锁顺序。
        product_ids = select(skus.c.product_id).where(
            skus.c.sku_id.in_([line["sku_id"] for line in row["lines"]])
        )
        session.execute(
            select(products.c.product_id)
            .where(products.c.product_id.in_(product_ids), products.c.store_id == ctx.store_id)
            .order_by(products.c.product_id)
            .with_for_update()
        ).all()
    current = {}
    if session.info.get("clock"):
        now = session.info["clock"].now()
        if row["expires_at"] <= now:
            raise AppError("quote_expired", 409, "报价已失效，请重新结算")
    for line in sorted(row["lines"], key=lambda item: item["sku_id"]):
        sku = sku_row(session, line["sku_id"], ctx.store_id, lock=lock)
        if session.info.get("clock"):
            now = session.info["clock"].now()
        if (
            sku["price_version"] != line["price_version"]
            or sku["product_version"] != line["product_version"]
            or str(sku["price_minor"]) != line["unit_price_minor"]
        ):
            raise AppError("quote_stale", 409, "价格或商品信息已变化，请重新查看报价")
        from cs_agent.activities.pricing import resolve_price

        pricing = resolve_price(session, ctx.store_id, sku, now)
        if (pricing["activity_id"] != line.get("activity_id")
                or pricing["activity_revision"] != line.get("activity_revision")
                or str(pricing["effective_price_minor"]) != line.get("effective_price_minor", line["unit_price_minor"])):
            raise AppError("quote_stale", 409, "活动或报价已变化，请重新结算")
        ensure_salable(sku, line["qty"])
        current[sku["sku_id"]] = sku
    shipping_input_row(session, ctx, row["shipping_input_id"])
    return current


def snapshot(session, ctx, order_id, input_row, snapshot_id, version, now):
    fields = {
        name: getattr(input_row, name)
        for name in ("recipient", "phone", "province", "city", "district", "street", "postal_code")
    }
    data = provenance(ctx, now)
    data["version"] = version
    session.execute(
        shipping_snapshots.insert().values(
            snapshot_id=snapshot_id,
            order_id=order_id,
            owner_id=ctx.subject_id,
            input_id=input_row.input_id,
            **fields,
            **data,
        )
    )


class CheckoutService:
    def cart(self, session, ctx, now):
        require_role(ctx, Role.CUSTOMER)
        cart = (
            session.execute(
                select(carts).where(
                    carts.c.owner_id == ctx.subject_id,
                    carts.c.store_id == ctx.store_id,
                    carts.c.data_batch_id == ctx.data_batch_id,
                    carts.c.batch_generation == ctx.batch_generation,
                )
            )
            .mappings()
            .first()
        )
        if cart is None:
            raise AppError("unavailable", 503, "购物车尚未初始化")
        result = []
        for item in session.execute(
            select(cart_items)
            .where(cart_items.c.cart_id == cart["cart_id"])
            .order_by(cart_items.c.created_at)
        ).mappings():
            sku = sku_row(session, item["sku_id"], ctx.store_id)
            from cs_agent.activities.pricing import resolve_price
            pricing = resolve_price(session, ctx.store_id, sku, now)
            result.append(
                {
                    "item_id": item["item_id"],
                    "sku_id": item["sku_id"],
                    "qty": item["qty"],
                    "selected": item["selected"],
                    "version": item["version"],
                    "name": sku["name"],
                    "specifications": sku["specifications"],
                    "image_slug": sku["image_slug"],
                    "price_minor": str(pricing["effective_price_minor"]),
                    "base_price_minor": str(pricing["base_price_minor"]),
                    "activity_id": pricing["activity_id"],
                    "activity_revision": pricing["activity_revision"],
                    "currency": "CNY",
                    "available_qty": sku["stock_qty"] - sku["reserved_qty"],
                    "status": sku["status"],
                    "product_status": sku["product_status"],
                }
            )
        return {"cart_id": cart["cart_id"], "version": cart["version"], "items": result}

    def put_cart_item(self, session, ctx, sku_id, qty, selected, expected_version, key, now):
        require_role(ctx, Role.CUSTOMER)

        def execute(op_id):
            cart = (
                session.execute(
                    select(carts)
                    .where(carts.c.owner_id == ctx.subject_id, carts.c.store_id == ctx.store_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            current = (
                session.execute(
                    select(cart_items).where(
                        cart_items.c.cart_id == cart["cart_id"], cart_items.c.sku_id == sku_id
                    )
                )
                .mappings()
                .first()
            )
            if (current["version"] if current else 0) != expected_version:
                raise AppError("version_conflict", 409, "购物车已更新，请刷新后重试")
            sku = sku_row(session, sku_id, ctx.store_id)
            ensure_salable(sku, qty)
            if current:
                item_id = current["item_id"]
                session.execute(
                    update(cart_items)
                    .where(cart_items.c.item_id == item_id)
                    .values(
                        qty=qty, selected=selected, version=current["version"] + 1, updated_at=now
                    )
                )
            else:
                count = len(
                    session.execute(
                        select(cart_items.c.item_id).where(cart_items.c.cart_id == cart["cart_id"])
                    ).all()
                )
                if count >= 50:
                    raise AppError("invalid_input", 422, "购物车单次支持最多50种规格")
                item_id = new_id("cartitem")
                session.execute(
                    cart_items.insert().values(
                        item_id=item_id,
                        cart_id=cart["cart_id"],
                        sku_id=sku_id,
                        qty=qty,
                        selected=selected,
                        **provenance(ctx, now),
                    )
                )
            session.execute(
                update(carts)
                .where(carts.c.cart_id == cart["cart_id"])
                .values(version=cart["version"] + 1, updated_at=now)
            )
            record(
                session,
                request_id=ctx.request_id,
                action="cart.updated",
                outcome="succeeded",
                ctx=ctx,
                resource_ref=item_id,
                summary={"version": cart["version"] + 1},
            )
            return {"business_ref": item_id, "version": (current["version"] + 1 if current else 1)}

        return execute_once(
            session,
            ctx,
            "cart.put",
            key,
            {
                "sku_id": sku_id,
                "qty": qty,
                "selected": selected,
                "expected_version": expected_version,
            },
            now,
            execute,
            resource_ref=sku_id,
        )

    def remove_cart_item(self, session, ctx, sku_id, expected_version, key, now):
        require_role(ctx, Role.CUSTOMER)

        def execute(op_id):
            cart = (
                session.execute(
                    select(carts)
                    .where(carts.c.owner_id == ctx.subject_id, carts.c.store_id == ctx.store_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            item = (
                session.execute(
                    select(cart_items).where(
                        cart_items.c.cart_id == cart["cart_id"], cart_items.c.sku_id == sku_id
                    )
                )
                .mappings()
                .first()
            )
            if item is None:
                raise not_found()
            check_version(item, expected_version)
            session.execute(delete(cart_items).where(cart_items.c.item_id == item["item_id"]))
            session.execute(
                update(carts)
                .where(carts.c.cart_id == cart["cart_id"])
                .values(version=cart["version"] + 1, updated_at=now)
            )
            return {"business_ref": item["item_id"]}

        return execute_once(
            session,
            ctx,
            "cart.remove",
            key,
            {"sku_id": sku_id, "expected_version": expected_version},
            now,
            execute,
            resource_ref=sku_id,
        )

    def create_shipping_input(self, session, ctx, fields, key, now):
        require_role(ctx, Role.CUSTOMER)

        def execute(op_id):
            row = ShippingInput(
                input_id=new_id("shipping"),
                owner_id=ctx.subject_id,
                **fields,
                **provenance(ctx, now),
            )
            if support_contact(contact_from(row))["address_masked"] == "****":
                raise AppError(
                    "invalid_input", 422, "请在省市字段填写行政区，在详细地址填写区县后的收货信息"
                )
            session.add(row)
            session.flush()
            record(
                session,
                request_id=ctx.request_id,
                action="shipping_input.created",
                outcome="succeeded",
                ctx=ctx,
                resource_ref=row.input_id,
                summary={"version": 1},
            )
            return {"business_ref": row.input_id, "version": 1}

        return execute_once(session, ctx, "shipping_input.create", key, fields, now, execute)

    def quote(self, session, ctx, items, shipping_input_ref, key, now, cart_item_ids=None):
        require_role(ctx, Role.CUSTOMER)
        cart_item_ids = sorted(cart_item_ids or [])
        if bool(items) == bool(cart_item_ids) or len(cart_item_ids) != len(set(cart_item_ids)):
            raise AppError("invalid_input", 422, "请选择一种结算方式和有效商品")

        def execute(op_id):
            shipping_input_row(session, ctx, shipping_input_ref, "checkout")
            bindings = []
            selected = items
            if cart_item_ids:
                cart = (
                    session.execute(
                        select(carts)
                        .where(carts.c.owner_id == ctx.subject_id, carts.c.store_id == ctx.store_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                rows = (
                    session.execute(
                        select(cart_items).where(
                            cart_items.c.cart_id == cart["cart_id"],
                            cart_items.c.item_id.in_(cart_item_ids),
                        )
                    )
                    .mappings()
                    .all()
                )
                if len(rows) != len(cart_item_ids):
                    raise not_found()
                if any(not row["selected"] for row in rows):
                    raise AppError("quote_stale", 409, "购物车选择已变化，请刷新")
                selected = [{"sku_id": row["sku_id"], "qty": row["qty"]} for row in rows]
                bindings = [
                    {
                        "cart_id": cart["cart_id"],
                        "cart_version": cart["version"],
                        "item_id": row["item_id"],
                        "item_version": row["version"],
                    }
                    for row in rows
                ]
            if not 1 <= len(selected) <= 50 or len({line["sku_id"] for line in selected}) != len(
                selected
            ):
                raise AppError("invalid_input", 422, "结算商品不能为空、重复或超过50种规格")
            lines = []
            for item in sorted(selected, key=lambda row: row["sku_id"]):
                sku = sku_row(session, item["sku_id"], ctx.store_id)
                ensure_salable(sku, item["qty"])
                from cs_agent.activities.pricing import resolve_price
                pricing = resolve_price(session, ctx.store_id, sku, now)
                amounts = line_amounts(pricing["base_price_minor"], pricing["effective_price_minor"], item["qty"])
                lines.append(
                    {
                        "sku_id": sku["sku_id"],
                        "product_name": sku["name"],
                        "specifications": sku["specifications"],
                        "image_slug": sku["image_slug"],
                        "qty": item["qty"],
                        "unit_price_minor": str(sku["price_minor"]),
                        **amounts,
                        "base_price_minor": str(pricing["base_price_minor"]),
                        "effective_price_minor": str(pricing["effective_price_minor"]),
                        "activity_id": pricing["activity_id"],
                        "activity_revision": pricing["activity_revision"],
                        "price_version": sku["price_version"],
                        "product_version": sku["product_version"],
                    }
                )
            total, discount, payable = quote_amounts(lines)
            policy = {**POLICY, "activity_pricing_revision": "XOPS-DIRECT-v0.1", "activity_lines": [
                {name: line[name] for name in ("sku_id", "base_price_minor", "effective_price_minor", "activity_id", "activity_revision")}
                for line in lines if line["activity_id"] is not None
            ]}
            quote_id = new_id("quote")
            expires = now + timedelta(minutes=5)
            session.execute(
                quotes.insert().values(
                    quote_id=quote_id,
                    owner_id=ctx.subject_id,
                    shipping_input_id=shipping_input_ref,
                    lines=lines,
                    cart_bindings=bindings,
                    currency="CNY",
                    goods_minor=total,
                    discount_minor=discount,
                    freight_minor=0,
                    payable_minor=payable,
                    policy_snapshot=policy,
                    expires_at=expires,
                    status="active",
                    **provenance(ctx, now),
                )
            )
            return {
                "business_ref": quote_id,
                "quote_id": quote_id,
                "version": 1,
                "lines": lines,
                "goods_minor": str(total),
                "discount_minor": str(discount),
                "freight_minor": "0",
                "payable_minor": str(payable),
                "currency": "CNY",
                "expires_at": expires.isoformat(),
                "policy_revision": POLICY["revision"],
                "shipping_input_ref": shipping_input_ref,
            }

        return execute_once(
            session,
            ctx,
            "checkout.quote",
            key,
            {
                "items": items,
                "cart_item_ids": cart_item_ids,
                "shipping_input_ref": shipping_input_ref,
            },
            now,
            execute,
        )

    def create_order(self, session, ctx, parameters, expected_version, op_id, now):
        quote = quote_row(session, ctx, parameters["quote_ref"], lock=True)
        check_version(quote, expected_version)
        current = validate_quote(session, ctx, quote, now, lock=True)
        order_id, snapshot_id = new_id("order"), new_id("snapshot")
        session.execute(
            orders.insert().values(
                order_id=order_id,
                owner_id=ctx.subject_id,
                display_no="SIM" + order_id[-16:].upper(),
                quote_id=quote["quote_id"],
                trade_status="awaiting_payment",
                fulfillment_status="unshipped",
                currency="CNY",
                goods_minor=quote["goods_minor"],
                discount_minor=quote["discount_minor"],
                freight_minor=quote["freight_minor"],
                payable_minor=quote["payable_minor"],
                paid_minor=0,
                refunded_minor=0,
                refunded_goods_minor=0,
                refunded_freight_minor=0,
                policy_snapshot=quote["policy_snapshot"],
                current_shipping_id=snapshot_id,
                change_count=0,
                payment_expires_at=now + timedelta(minutes=15),
                **provenance(ctx, now),
            )
        )
        snapshot(
            session,
            ctx,
            order_id,
            shipping_input_row(session, ctx, quote["shipping_input_id"]),
            snapshot_id,
            1,
            now,
        )
        from cs_agent.activities.pricing import reserve_order

        reserve_order(session, ctx, order_id, quote["lines"], now)
        for line in quote["lines"]:
            amount = int(line["allocated_minor"])
            base, remaining = divmod(amount, line["qty"])
            session.execute(
                order_items.insert().values(
                    order_item_id=new_id("item"),
                    order_id=order_id,
                    sku_id=line["sku_id"],
                    product_name=line["product_name"],
                    specifications=line["specifications"],
                    image_slug=line["image_slug"],
                    unit_price_minor=int(line["unit_price_minor"]),
                    ordered_qty=line["qty"],
                    goods_minor=int(line["goods_minor"]),
                    discount_minor=int(line["discount_minor"]),
                    allocated_minor=amount,
                    paid_goods_minor=0,
                    unit_allocations={
                        "base_minor": str(base),
                        "tail_minor": str(base + remaining),
                        "quantity": line["qty"],
                    },
                    allocation_revision="ALLOC-LAST-UNIT-v1",
                    **provenance(ctx, now),
                )
            )
            move_stock(
                session,
                ctx,
                current[line["sku_id"]],
                0,
                line["qty"],
                "reservation",
                order_id,
                op_id,
                now,
                "order_reservation",
            )
            session.execute(
                reservations.insert().values(
                    reservation_id=new_id("reservation"),
                    order_id=order_id,
                    sku_id=line["sku_id"],
                    qty=line["qty"],
                    status="reserved",
                    expires_at=now + timedelta(minutes=15),
                    **provenance(ctx, now),
                )
            )
        for binding in quote["cart_bindings"]:
            session.execute(
                delete(cart_items).where(
                    cart_items.c.item_id == binding["item_id"],
                    cart_items.c.version == binding["item_version"],
                )
            )
        if quote["cart_bindings"]:
            session.execute(
                update(carts)
                .where(carts.c.cart_id == quote["cart_bindings"][0]["cart_id"])
                .values(version=carts.c.version + 1, updated_at=now)
            )
        session.execute(
            update(quotes)
            .where(quotes.c.quote_id == quote["quote_id"])
            .values(status="consumed", version=2, updated_at=now)
        )
        event(
            session,
            ctx,
            "order.created",
            order_id,
            1,
            order_id,
            {"order_ref": order_id, "payable_minor": str(quote["payable_minor"])},
            now,
        )
        record(
            session,
            request_id=ctx.request_id,
            action="order.created",
            outcome="succeeded",
            ctx=ctx,
            resource_ref=order_id,
            summary={"version": 1},
        )
        return {
            "business_ref": order_id,
            "version": 1,
            "payable_minor": str(quote["payable_minor"]),
            "currency": "CNY",
        }

    def pay(self, session, ctx, order_id, expected_version, op_id, now):
        row = owned_order(session, ctx, order_id, lock=True)
        if session.info.get("clock"):
            now = session.info["clock"].now()
        check_version(row, expected_version)
        if row["trade_status"] != "awaiting_payment" or row["payment_expires_at"] <= now:
            raise AppError("state_conflict", 409, "订单已关闭、已付款或付款时间已过，请刷新")
        held = (
            session.execute(
                select(reservations)
                .where(reservations.c.order_id == order_id)
                .order_by(reservations.c.sku_id)
            )
            .mappings()
            .all()
        )
        locked = {
            item["sku_id"]: sku_row(session, item["sku_id"], ctx.store_id, lock=True)
            for item in held
        }
        if not held or any(item["status"] != "reserved" for item in held):
            raise AppError("state_conflict", 409, "订单库存预留已失效")
        # 账户行也可能等待；获得所有资金/库存锁后重新判断原付款期。
        from cs_agent.wallet.service import account_row

        account_row(session, ctx, lock=True)
        if session.info.get("clock"):
            now = session.info["clock"].now()
            if row["payment_expires_at"] <= now:
                raise AppError("state_conflict", 409, "订单付款时间已过，请刷新")
        entry_id, balance, _ = post_entry(
            session, ctx, row["payable_minor"], "payment", op_id, order_id, now
        )
        for item in held:
            move_stock(
                session,
                ctx,
                locked[item["sku_id"]],
                -item["qty"],
                -item["qty"],
                "payment",
                order_id,
                op_id,
                now,
                "paid_inventory_consumption",
            )
        session.execute(
            update(reservations)
            .where(reservations.c.order_id == order_id)
            .values(status="consumed", version=reservations.c.version + 1, updated_at=now)
        )
        session.execute(
            update(order_items)
            .where(order_items.c.order_id == order_id)
            .values(paid_goods_minor=order_items.c.allocated_minor, updated_at=now)
        )
        session.execute(
            payments.insert().values(
                payment_id=new_id("payment"),
                order_id=order_id,
                operation_id=op_id,
                entry_id=entry_id,
                amount_minor=row["payable_minor"],
                currency="CNY",
                status="succeeded",
                paid_at=now,
                **provenance(ctx, now),
            )
        )
        session.execute(
            update(orders)
            .where(orders.c.order_id == order_id)
            .values(
                trade_status="paid",
                paid_minor=row["payable_minor"],
                paid_at=now,
                version=row["version"] + 1,
                updated_at=now,
            )
        )
        from cs_agent.activities.pricing import consume_order

        consume_order(session, ctx, order_id, now)
        event(
            session,
            ctx,
            "order.paid",
            order_id,
            row["version"] + 1,
            order_id,
            {"order_ref": order_id, "paid_minor": str(row["payable_minor"])},
            now,
        )
        return {
            "business_ref": order_id,
            "version": row["version"] + 1,
            "paid_minor": str(row["payable_minor"]),
            "balance_minor": str(balance),
            "currency": "CNY",
        }
