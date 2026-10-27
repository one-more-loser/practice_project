"""超时任务只使用数据库业务记录，可重试，不依赖页面在线。"""

from sqlalchemy import select, update

from cs_agent.audit.service import record
from cs_agent.catalog.service import sku_row
from cs_agent.core.context import Role, TrustedContext
from cs_agent.db.business_tables import orders, reservations
from cs_agent.inventory.service import move_stock
from cs_agent.wallet.service import event


def release_unpaid(session, ctx, row, now, reason, op_id=None):
    held = (
        session.execute(
            select(reservations)
            .where(reservations.c.order_id == row["order_id"], reservations.c.status == "reserved")
            .order_by(reservations.c.sku_id)
        )
        .mappings()
        .all()
    )
    for item in held:
        sku = sku_row(session, item["sku_id"], ctx.store_id, lock=True)
        move_stock(
            session, ctx, sku, 0, -item["qty"], "release", row["order_id"], op_id, now, reason
        )
    session.execute(
        update(reservations)
        .where(reservations.c.order_id == row["order_id"], reservations.c.status == "reserved")
        .values(status="released", version=reservations.c.version + 1, updated_at=now)
    )
    session.execute(
        update(orders)
        .where(orders.c.order_id == row["order_id"])
        .values(
            trade_status="cancelled_unpaid",
            cancelled_at=now,
            cancellation_reason=reason,
            version=row["version"] + 1,
            updated_at=now,
        )
    )
    from cs_agent.activities.pricing import release_order

    release_order(session, ctx, row["order_id"], now)
    event(
        session,
        ctx,
        "order.cancelled_unpaid",
        row["order_id"],
        row["version"] + 1,
        row["order_id"],
        {"order_ref": row["order_id"], "reason_code": reason},
        now,
    )
    record(
        session,
        request_id=ctx.request_id,
        action="order.cancelled_unpaid",
        outcome="succeeded",
        ctx=ctx,
        resource_ref=row["order_id"],
        summary={"reason_code": reason, "version": row["version"] + 1},
    )


def expire_due(session, now, limit=100):
    rows = (
        session.execute(
            select(orders)
            .where(orders.c.trade_status == "awaiting_payment", orders.c.payment_expires_at <= now)
            .order_by(orders.c.order_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .mappings()
        .all()
    )
    # 批量任务也按全批SKU排序，不能按每单顺序获得相反库存锁。
    refs = [row["order_id"] for row in rows]
    held = session.execute(
        select(reservations.c.sku_id, reservations.c.store_id)
        .where(reservations.c.order_id.in_(refs), reservations.c.status == "reserved")
        .distinct()
        .order_by(reservations.c.sku_id)
    ).all()
    for sku_id, store_id in held:
        sku_row(session, sku_id, store_id, lock=True)
    # SKU后锁整批活动并全局排序；两批多活动订单不能按各自订单顺序交叉取锁。
    from cs_agent.db.activity_tables import activities, allocations

    activity_ids = select(allocations.c.activity_id).where(allocations.c.order_id.in_(refs))
    session.execute(select(activities.c.activity_id).where(
        activities.c.activity_id.in_(activity_ids)).order_by(activities.c.activity_id).with_for_update()).all()
    for row in rows:
        # 任务来源单独标记；不冒充顾客确认，也不执行有资金变化的动作。
        ctx = TrustedContext(
            subject_id=row["owner_id"],
            store_id=row["store_id"],
            role=Role.CUSTOMER,
            session_ref="system_unpaid_expiration",
            auth_version=0,
            data_batch_id=row["data_batch_id"],
            batch_generation=row["batch_generation"],
            request_id="job_unpaid_expiration",
        )
        release_unpaid(session, ctx, row, now, "unpaid_timeout")
    return len(rows)
