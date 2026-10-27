import re
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from cs_agent.actions.repository import fingerprint, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.cancellation.review import RULES, SKILL
from cs_agent.commerce.checkout import check_version, owned_order
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.business_tables import (
    confirmations,
    inventory,
    operations,
    order_items,
    orders,
    payments,
    reservations,
    shipments,
    wallet_entries,
    wallets,
)
from cs_agent.db.cancellation_tables import refunds, reviews
from cs_agent.identity.policies import require_role
from cs_agent.inventory.service import move_stock
from cs_agent.wallet.service import event, post_entry


def scope(table, ctx):
    return (
        table.c.store_id == ctx.store_id,
        table.c.data_batch_id == ctx.data_batch_id,
        table.c.batch_generation == ctx.batch_generation,
    )


def submission_parameters(row, decision):
    return {
        "confirmation_id": row["confirmation_id"],
        "decision": decision,
        "parameters_digest": row["parameters_digest"],
        "expected_version": row["expected_version"],
    }


def validate_confirmation(row, ctx):
    if (
        row["owner_id"] != ctx.subject_id
        or row["actor_role"] != Role.CUSTOMER.value
        or row["store_id"] != ctx.store_id
        or row["data_batch_id"] != ctx.data_batch_id
        or row["batch_generation"] != ctx.batch_generation
    ):
        raise not_found()
    if row["auth_version"] != ctx.auth_version:
        raise AppError("state_conflict", 409, "授权已变化，请重新确认")
    if (
        row["action"] != "order.cancel"
        or row["summary"].get("requires_agent_review") is not True
        or row["parameters"] != {"order_ref": row["resource_ref"]}
        or row["parameters_digest"] != fingerprint(row["parameters"])
    ):
        raise AppError("invalid_input", 422, "取消确认与原操作摘要不一致")


class CancellationService:
    def submit(self, session, ctx, confirmation_row, decision, key, now):
        """短事务接收action任务；与旧execute_once的同步成功路径分开。"""
        require_role(ctx, Role.CUSTOMER)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
            raise AppError("invalid_input", 422, "请提供有效的操作编号")
        if decision not in {"confirm", "cancel"}:
            raise AppError("invalid_input", 422, "确认选项不符合契约")
        row = (
            session.execute(
                select(confirmations)
                .where(
                    confirmations.c.confirmation_id == confirmation_row["confirmation_id"],
                    confirmations.c.owner_id == ctx.subject_id,
                    confirmations.c.actor_role == ctx.role.value,
                    *scope(confirmations, ctx),
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        validate_confirmation(row, ctx)
        from cs_agent.actions.origin import verify_origin

        digest = fingerprint(submission_parameters(row, decision))

        def replay():
            base = (
                operations.c.actor_id == ctx.subject_id,
                operations.c.actor_role == ctx.role.value,
                operations.c.action == "order.cancel",
                *scope(operations, ctx),
            )
            # 键冲突优先于原确认回放：不能用另一个已占用的键悄悄命中原确认。
            by_key = (
                session.execute(
                    select(operations).where(
                        *base,
                        operations.c.idempotency_key == key,
                    )
                )
                .mappings()
                .first()
            )
            by_confirmation = (
                session.execute(
                    select(operations).where(
                        *base,
                        operations.c.confirmation_id == row["confirmation_id"],
                    )
                )
                .mappings()
                .first()
            )
            for original in (by_key, by_confirmation):
                if original and original["parameters_digest"] != digest:
                    raise AppError(
                        "idempotency_conflict", 409, "操作编号已用于其他内容，请核对原操作"
                    )
            original = by_key or by_confirmation
            return (original["result"], original["http_status"]) if original else None

        existing = replay()
        if existing:
            return existing
        op_id = new_id("op")
        created = session.execute(
            insert(operations)
            .values(
                operation_id=op_id,
                actor_id=ctx.subject_id,
                actor_role=ctx.role.value,
                action="order.cancel",
                idempotency_key=key,
                parameters_digest=digest,
                resource_ref=row["resource_ref"],
                confirmation_id=row["confirmation_id"],
                status="accepted",
                result={},
                http_status=202,
                **provenance(ctx, now),
            )
            .on_conflict_do_nothing()
            .returning(operations.c.operation_id)
        ).scalar_one_or_none()
        if not created:
            original = replay()
            if original:
                return original
            raise AppError("idempotency_conflict", 409, "请核对原操作")
        try:
            with session.begin_nested():
                verify_origin(session, row["confirmation_id"], ctx)
                if (
                    row["status"] != "pending"
                    or row["expires_at"] <= now
                    or now - row["created_at"] > timedelta(minutes=5)
                ):
                    raise AppError("confirmation_expired", 409, "确认已失效，请重新核对操作")
                if decision == "cancel":
                    result = {
                        "operation_id": op_id,
                        "status": "succeeded",
                        "business_ref": row["resource_ref"],
                        "decision": "cancel",
                        "business_effect": "none",
                    }
                    status = 200
                else:
                    order = owned_order(session, ctx, row["resource_ref"], lock=True)
                    check_version(order, row["expected_version"])
                    ensure_paid_unshipped(order)
                    review_id = new_id("cancelreview")
                    session.execute(
                        reviews.insert().values(
                            review_id=review_id,
                            task_kind="action",
                            operation_id=op_id,
                            confirmation_id=row["confirmation_id"],
                            order_id=order["order_id"],
                            owner_id=ctx.subject_id,
                            session_ref=ctx.session_ref,
                            auth_version=ctx.auth_version,
                            request_id=ctx.request_id,
                            expected_order_version=order["version"],
                            status="queued",
                            rules_revision=RULES,
                            skill_revision=SKILL,
                            confirmed_at=now,
                            **provenance(ctx, now),
                        )
                    )
                    result = {
                        "operation_id": op_id,
                        "status": "accepted",
                        "business_ref": order["order_id"],
                        "review_id": review_id,
                        "review_status": "awaiting_agent_review",
                        "business_effect": "none",
                    }
                    status = 202
                session.execute(
                    update(confirmations)
                    .where(
                        confirmations.c.confirmation_id == row["confirmation_id"],
                    )
                    .values(
                        status="cancelled" if decision == "cancel" else "confirmed",
                        confirmed_at=now,
                        version=row["version"] + 1,
                        updated_at=now,
                    )
                )
        except AppError as exc:
            result, status = rejected_result(
                op_id, row["resource_ref"], exc.code, exc.message, exc.status
            )
        session.execute(
            update(operations)
            .where(operations.c.operation_id == op_id)
            .values(
                result=result,
                http_status=status,
                status=result["status"],
                updated_at=now,
            )
        )
        record(
            session,
            request_id=ctx.request_id,
            action="cancel.submitted",
            outcome="succeeded" if status < 400 else "denied",
            ctx=ctx,
            resource_ref=row["confirmation_id"],
            summary={"version": row["expected_version"]},
        )
        return result, status

    def get(self, session, ctx, review_id):
        require_role(ctx, Role.CUSTOMER)
        row = (
            session.execute(
                select(reviews).where(
                    reviews.c.review_id == review_id,
                    reviews.c.owner_id == ctx.subject_id,
                    *scope(reviews, ctx),
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise not_found()
        return public_review(row)

    def list(self, session, ctx, order_ref=None, limit=20, offset=0):
        require_role(ctx, Role.CUSTOMER)
        stmt = select(reviews).where(reviews.c.owner_id == ctx.subject_id, *scope(reviews, ctx))
        if order_ref:
            owned_order(session, ctx, order_ref)
            stmt = stmt.where(reviews.c.order_id == order_ref)
        rows = session.execute(
            stmt.order_by(reviews.c.created_at.desc(), reviews.c.review_id)
            .limit(limit)
            .offset(offset)
        ).mappings()
        return [public_review(row) for row in rows]


def public_review(row):
    fields = (
        "review_id",
        "operation_id",
        "order_id",
        "status",
        "decision",
        "reason_codes",
        "basis_summary",
        "rules_revision",
        "skill_revision",
        "model_identifier",
        "model_revision",
        "error_code",
        "refund_id",
        "entry_id",
        "version",
    )
    output = {name: row[name] for name in fields}
    output.update(
        {
            name: row[name].isoformat() if row[name] else None
            for name in (
                "confirmed_at",
                "reviewed_at",
                "executed_at",
                "created_at",
            )
        }
    )
    output["needs_assistance"] = row["status"] in {"needs_facts", "unavailable"}
    return output


def rejected_result(op_id, order_id, code, message, http_status=409, review_id=None):
    result = {
        "operation_id": op_id,
        "status": "rejected",
        "business_ref": order_id,
        "business_effect": "none",
        "error": {"code": code, "message": message, "retryable": False},
    }
    if review_id:
        result["review_id"] = review_id
    return result, http_status


def ensure_paid_unshipped(order):
    if (
        order["trade_status"] != "paid"
        or order["fulfillment_status"] != "unshipped"
        or order["shipped_at"] is not None
        or order["paid_minor"] <= 0
        or order["refunded_minor"] != 0
    ):
        raise AppError("state_conflict", 409, "当前订单不满足已付未发货普通取消条件")


def load_facts(session, ctx, review, *, lock=False):
    """只读取白名单事实。订单锁串行化发货/改址/取消，模型等待在事务外。"""
    order = owned_order(session, ctx, review["order_id"], lock=lock)
    check_version(order, review["expected_order_version"])
    ensure_paid_unshipped(order)
    payment = (
        session.execute(
            select(payments).where(
                payments.c.order_id == order["order_id"],
                *scope(payments, ctx),
            )
        )
        .mappings()
        .first()
    )
    shipment_rows = (
        session.execute(
            select(shipments).where(
                shipments.c.order_id == order["order_id"],
                *scope(shipments, ctx),
            )
        )
        .mappings()
        .all()
    )
    if shipment_rows:
        raise AppError("state_conflict", 409, "已有发货事实，不能普通取消")
    entry = (
        session.execute(
            select(wallet_entries).where(
                wallet_entries.c.entry_id == (payment["entry_id"] if payment else ""),
                wallet_entries.c.owner_id == ctx.subject_id,
                *scope(wallet_entries, ctx),
            )
        )
        .mappings()
        .first()
    )
    account = (
        session.execute(
            select(wallets).where(
                wallets.c.account_id == (entry["account_id"] if entry else ""),
                wallets.c.owner_id == ctx.subject_id,
                *scope(wallets, ctx),
            )
        )
        .mappings()
        .first()
    )
    if (
        not payment
        or not entry
        or not account
        or payment["status"] != "succeeded"
        or payment["currency"] != "CNY"
        or entry["currency"] != "CNY"
        or entry["kind"] != "payment"
        or entry["business_ref"] != order["order_id"]
        or entry["operation_id"] != payment["operation_id"]
        or payment["amount_minor"] != order["paid_minor"]
        or entry["amount_minor"] != order["paid_minor"]
        or order["paid_minor"] != order["payable_minor"]
    ):
        raise AppError(
            "cancellation_facts_incomplete", 409, "原付款与本人账务事实不一致，请协助核验"
        )
    item_stmt = (
        select(order_items)
        .where(order_items.c.order_id == order["order_id"], *scope(order_items, ctx))
        .order_by(order_items.c.order_item_id)
    )
    held_stmt = (
        select(reservations)
        .where(reservations.c.order_id == order["order_id"], *scope(reservations, ctx))
        .order_by(reservations.c.sku_id)
    )
    items = session.execute(item_stmt.with_for_update() if lock else item_stmt).mappings().all()
    held = session.execute(held_stmt.with_for_update() if lock else held_stmt).mappings().all()
    quantities = {}
    for item in items:
        quantities[item["sku_id"]] = quantities.get(item["sku_id"], 0) + item["ordered_qty"]
    if (
        not items
        or len(held) != len(quantities)
        or {row["sku_id"]: row["qty"] for row in held} != quantities
        or any(row["status"] != "consumed" for row in held)
        or any(
            item["refunded_qty"]
            or item["reserved_refund_qty"]
            or item["refunded_goods_minor"]
            or item["reserved_refund_minor"]
            for item in items
        )
        or sum(item["paid_goods_minor"] for item in items) + order["freight_minor"]
        != order["paid_minor"]
    ):
        raise AppError(
            "cancellation_facts_incomplete", 409, "订单明细或未履约库存依据不足，请协助核验"
        )
    facts = {
        "order_ref": order["order_id"],
        "order_version": order["version"],
        "confirmation_ref": review["confirmation_id"],
        "confirmation_verified": True,
        "owner_matches_confirmation": True,
        "auth_version": ctx.auth_version,
        "trade_status": order["trade_status"],
        "fulfillment_status": order["fulfillment_status"],
        "paid_minor": str(order["paid_minor"]),
        "refundable_minor": str(order["paid_minor"]),
        "freight_minor": str(order["freight_minor"]),
        "currency": "CNY",
        "payment_ref": payment["payment_id"],
        "payment_version": payment["version"],
        "payment_entry_ref": entry["entry_id"],
        "payment_entry_version": entry["version"],
        "shipment_refs": [],
        "policy_revision": order["policy_snapshot"].get("revision"),
        "items": [
            {
                "item_ref": item["order_item_id"],
                "version": item["version"],
                "sku_ref": item["sku_id"],
                "qty": item["ordered_qty"],
                "paid_goods_minor": str(item["paid_goods_minor"]),
            }
            for item in items
        ],
        "stock_sources": [
            {
                "reservation_ref": row["reservation_id"],
                "version": row["version"],
                "sku_ref": row["sku_id"],
                "qty": row["qty"],
                "status": row["status"],
            }
            for row in held
        ],
    }
    return facts, order, payment, items, held


def execute_refund(session, ctx, review, now):
    facts, order, payment, items, held = load_facts(session, ctx, review, lock=True)
    if fingerprint(facts) != review["facts_digest"]:
        raise AppError("cancellation_facts_changed", 409, "审核依据已变化，请重新核对并确认")
    if (
        review["decision"] != "approved"
        or review["rules_revision"] != RULES
        or review["skill_revision"] != SKILL
    ):
        raise AppError("cancellation_review_required", 409, "缺少有效的Agent审核通过记录")
    # 与支付相同：订单→按SKU库存→本人账户。全部效果在一个事务提交。
    locked = {}
    for item in held:
        stock = (
            session.execute(
                select(inventory)
                .where(
                    inventory.c.sku_id == item["sku_id"],
                    *scope(inventory, ctx),
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if stock is None:
            raise AppError("cancellation_facts_incomplete", 409, "缺少原订单库存来源")
        locked[item["sku_id"]] = {**stock, "inventory_version": stock["version"]}
    refund_id = new_id("cancelrefund")
    entry_id, balance, _ = post_entry(
        session, ctx, order["paid_minor"], "refund", review["operation_id"], refund_id, now
    )
    for item in held:
        move_stock(
            session,
            ctx,
            locked[item["sku_id"]],
            item["qty"],
            0,
            "cancellation_restock",
            refund_id,
            review["operation_id"],
            now,
            "approved_unshipped_cancellation",
        )
    for item in items:
        session.execute(
            update(order_items)
            .where(order_items.c.order_item_id == item["order_item_id"])
            .values(
                refunded_qty=item["ordered_qty"],
                refunded_goods_minor=item["paid_goods_minor"],
                version=item["version"] + 1,
                updated_at=now,
            )
        )
    goods = order["paid_minor"] - order["freight_minor"]
    from cs_agent.activities.pricing import record_refund

    record_refund(session, ctx, refund_id, order["order_id"], order["paid_minor"], now,
                  allocations=[{"sku_id": item["sku_id"], "amount_minor": str(item["paid_goods_minor"])}
                               for item in items])
    session.execute(
        refunds.insert().values(
            refund_id=refund_id,
            review_id=review["review_id"],
            operation_id=review["operation_id"],
            order_id=order["order_id"],
            owner_id=ctx.subject_id,
            payment_id=payment["payment_id"],
            payment_entry_id=payment["entry_id"],
            entry_id=entry_id,
            amount_minor=order["paid_minor"],
            goods_minor=goods,
            freight_minor=order["freight_minor"],
            currency="CNY",
            refunded_at=now,
            **provenance(ctx, now),
        )
    )
    session.execute(
        update(orders)
        .where(orders.c.order_id == order["order_id"])
        .values(
            trade_status="cancelled_refunded",
            refunded_minor=order["paid_minor"],
            refunded_goods_minor=goods,
            refunded_freight_minor=order["freight_minor"],
            cancelled_at=now,
            cancellation_reason="agent_approved_customer_cancellation",
            version=order["version"] + 1,
            updated_at=now,
        )
    )
    event(
        session,
        ctx,
        "order.cancelled",
        order["order_id"],
        order["version"] + 1,
        review["operation_id"],
        {
            "order_ref": order["order_id"],
            "review_ref": review["review_id"],
            "refund_ref": refund_id,
            "refund_minor": str(order["paid_minor"]),
            "currency": "CNY",
        },
        now,
    )
    record(
        session,
        request_id=ctx.request_id,
        action="cancel.executed",
        outcome="succeeded",
        ctx=ctx,
        resource_ref=review["review_id"],
        summary={"version": order["version"] + 1},
    )
    return {
        "operation_id": review["operation_id"],
        "status": "succeeded",
        "business_ref": order["order_id"],
        "review_id": review["review_id"],
        "review_status": "succeeded",
        "refund_id": refund_id,
        "entry_id": entry_id,
        "refunded_minor": str(order["paid_minor"]),
        "balance_minor": str(balance),
        "currency": "CNY",
        "version": order["version"] + 1,
    }
