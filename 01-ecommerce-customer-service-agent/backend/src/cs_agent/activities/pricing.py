"""交易本地钩子。调用者保有SKU锁，再以活动ID排序加锁；不提交/不做HTTP。"""

from sqlalchemy import select, update

from cs_agent.actions.repository import new_id, provenance
from cs_agent.activities.contracts import VERSION
from cs_agent.activities.repository import enqueue, refresh_phase, scope
from cs_agent.core.errors import AppError
from cs_agent.db.activity_tables import activities, allocations, lines, refunds


def conflict():
    raise AppError("activity_requote_required", 409, "活动或可用额度已变化，请重新报价")


def fresh_now(session, now):
    clock = getattr(session, "info", {}).get("clock")
    return clock.now() if clock else now


def resolve_price(session, store_id, sku, now):
    base = sku["price_minor"]
    result = dict(
        base_price_minor=base,
        effective_price_minor=base,
        activity_id=None,
        activity_revision=None,
        price_version=sku["price_version"],
    )
    rows = (
        session.execute(
            select(
                activities,
                lines.c.effective_price_minor,
                lines.c.base_price_minor.label("line_base"),
                lines.c.price_version.label("line_price_version"),
            )
            .join(lines, lines.c.activity_id == activities.c.activity_id)
            .where(
                activities.c.store_id == store_id,
                activities.c.data_batch_id == sku["data_batch_id"],
                activities.c.batch_generation == sku["batch_generation"],
                lines.c.sku_id == sku["sku_id"],
                activities.c.phase.in_(["scheduled", "active"]),
                activities.c.start_at <= now,
                activities.c.end_at > now,
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > 1:
        conflict()
    if rows:
        row = rows[0]
        if row["line_base"] != base or row["line_price_version"] != sku["price_version"]:
            conflict()
        # 时间投影在订单/后台持久刷新；只读商品解析不写入或抢锁。
        result.update(
            effective_price_minor=row["effective_price_minor"],
            activity_id=row["activity_id"],
            activity_revision=row["revision"] + (1 if row["phase"] == "scheduled" else 0),
        )
    return result


def reject_sku_change(session, ctx, sku_id, now):
    row = session.execute(
        select(activities.c.activity_id)
        .join(lines, lines.c.activity_id == activities.c.activity_id)
        .where(
            *scope(activities, ctx),
            lines.c.sku_id == sku_id,
            activities.c.phase != "ended",
            activities.c.end_at > now,
        )
    ).first()
    if row:
        raise AppError("activity_price_conflict", 409, "商品存在未结束的活动，请先结束活动")


def assert_price_change_allowed(session, store_id, sku_id, now):
    # 调用方已锁现行SKU；只核对同店SKU活动，不重新取得活动锁。
    row = session.execute(
        select(activities.c.activity_id)
        .join(lines, lines.c.activity_id == activities.c.activity_id)
        .where(
            activities.c.store_id == store_id,
            lines.c.sku_id == sku_id,
            activities.c.phase != "ended",
            activities.c.end_at > now,
        )
    ).first()
    if row:
        raise AppError("activity_price_conflict", 409, "商品存在未结束的活动，请先结束活动")


def _lock_activities(session, ctx, ids, now):
    result = {}
    for aid in sorted(set(ids)):
        row = (
            session.execute(
                select(activities)
                .where(*scope(activities, ctx), activities.c.activity_id == aid)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if not row:
            conflict()
        result[aid] = refresh_phase(session, row, fresh_now(session, now))
    return result


def reserve_order(session, ctx, order_id, order_lines, now):
    selected = [r for r in order_lines if r.get("activity_id")]
    locked = _lock_activities(session, ctx, [r["activity_id"] for r in selected], now)
    now = fresh_now(session, now)
    for item in sorted(selected, key=lambda r: (r["activity_id"], r["sku_id"])):
        prior = (
            session.execute(
                select(allocations).where(
                    *scope(allocations, ctx),
                    allocations.c.order_id == order_id,
                    allocations.c.sku_id == item["sku_id"],
                )
            )
            .mappings()
            .first()
        )
        qty = item.get("qty", item.get("ordered_qty"))
        base = int(item["base_price_minor"])
        effective = int(item["effective_price_minor"])
        discount = (base - effective) * qty
        if prior:
            if (
                prior["activity_id"] != item["activity_id"]
                or prior["qty"] != qty
                or prior["discount_minor"] != discount
            ):
                conflict()
            continue
        act = locked[item["activity_id"]]
        if (
            act["phase"] != "active"
            or act["revision"] != item["activity_revision"]
            or not act["start_at"] <= now < act["end_at"]
        ):
            conflict()
        line = (
            session.execute(
                select(lines)
                .where(lines.c.activity_id == act["activity_id"], lines.c.sku_id == item["sku_id"])
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if (
            not line
            or type(qty) is not int
            or qty < 1
            or line["base_price_minor"] != base
            or line["effective_price_minor"] != effective
            or line["reserved_qty"] + line["consumed_qty"] + qty > line["quantity_cap"]
            or act["reserved_discount_minor"] + act["consumed_discount_minor"] + discount
            > act["variable_cap_minor"]
        ):
            conflict()
        session.execute(
            update(lines)
            .where(lines.c.line_id == line["line_id"])
            .values(reserved_qty=line["reserved_qty"] + qty, updated_at=now)
        )
        act = dict(act, reserved_discount_minor=act["reserved_discount_minor"] + discount)
        locked[act["activity_id"]] = act
        session.execute(
            update(activities)
            .where(activities.c.activity_id == act["activity_id"])
            .values(reserved_discount_minor=act["reserved_discount_minor"], updated_at=now)
        )
        session.execute(
            allocations.insert().values(
                allocation_id=new_id("aa"),
                activity_id=act["activity_id"],
                order_id=order_id,
                sku_id=item["sku_id"],
                qty=qty,
                discount_minor=discount,
                payment_minor=effective * qty,
                refund_minor=0,
                status="reserved",
                **provenance(ctx, now),
            )
        )
    for aid in locked:
        record_actuals(session, ctx, aid, now)


def _transition(session, ctx, order_id, now, target):
    rows = (
        session.execute(
            select(allocations)
            .where(*scope(allocations, ctx), allocations.c.order_id == order_id)
            .order_by(allocations.c.activity_id, allocations.c.sku_id)
        )
        .mappings()
        .all()
    )
    locked = _lock_activities(session, ctx, [r["activity_id"] for r in rows], now)
    now = fresh_now(session, now)
    # 活动锁后重新读取，避免两次付款/取消读到同一个旧reserved状态。
    rows = (
        session.execute(
            select(allocations)
            .where(*scope(allocations, ctx), allocations.c.order_id == order_id)
            .order_by(allocations.c.activity_id, allocations.c.sku_id)
            .with_for_update()
        )
        .mappings()
        .all()
    )
    for row in rows:
        if row["status"] == target:
            continue
        if row["status"] != "reserved":
            if target == "released" and row["status"] == "consumed":
                continue  # 已付退款不恢复营销额度。
            conflict()
        aid = row["activity_id"]
        act = locked[aid]
        line = (
            session.execute(
                select(lines)
                .where(lines.c.activity_id == aid, lines.c.sku_id == row["sku_id"])
                .with_for_update()
            )
            .mappings()
            .one()
        )
        consumed = target == "consumed"
        session.execute(
            update(lines)
            .where(lines.c.line_id == line["line_id"])
            .values(
                reserved_qty=line["reserved_qty"] - row["qty"],
                consumed_qty=line["consumed_qty"] + (row["qty"] if consumed else 0),
                updated_at=now,
            )
        )
        act = dict(
            act,
            reserved_discount_minor=act["reserved_discount_minor"] - row["discount_minor"],
            consumed_discount_minor=act["consumed_discount_minor"]
            + (row["discount_minor"] if consumed else 0),
        )
        locked[aid] = act
        session.execute(
            update(activities)
            .where(activities.c.activity_id == aid)
            .values(
                reserved_discount_minor=act["reserved_discount_minor"],
                consumed_discount_minor=act["consumed_discount_minor"],
                updated_at=now,
            )
        )
        session.execute(
            update(allocations)
            .where(allocations.c.allocation_id == row["allocation_id"])
            .values(status=target, updated_at=now)
        )
    for aid in locked:
        record_actuals(session, ctx, aid, now)


def consume_order(session, ctx, order_id, now):
    _transition(session, ctx, order_id, now, "consumed")


def release_order(session, ctx, order_id, now):
    _transition(session, ctx, order_id, now, "released")


def record_refund(session, ctx, refund_id, order_id, amount_minor, now, allocations=None):
    # 混合活动/全价订单必须由正式退款行提供已分摊金额，禁止按全订单金额猜分摊。
    from cs_agent.db.activity_tables import allocations as table

    rows = (
        session.execute(select(table).where(*scope(table, ctx), table.c.order_id == order_id))
        .mappings()
        .all()
    )
    if not rows:
        return
    if allocations is None or sum(int(r["amount_minor"]) for r in allocations) > int(amount_minor):
        raise AppError("activity_refund_allocation_required", 409, "退款需要正式订单行分摊")
    _lock_activities(session, ctx, [r["activity_id"] for r in rows], now)
    now = fresh_now(session, now)
    parts = {r["sku_id"]: int(r["amount_minor"]) for r in allocations}
    if len(parts) != len(allocations):
        conflict()
    for row in rows:
        amount = parts.get(row["sku_id"], 0)
        if type(amount) is not int or amount < 0 or row["status"] != "consumed":
            conflict()
        existing = (
            session.execute(
                select(refunds).where(
                    refunds.c.refund_id == refund_id,
                    refunds.c.allocation_id == row["allocation_id"],
                )
            )
            .mappings()
            .first()
        )
        if existing:
            if existing["amount_minor"] != amount:
                conflict()
            continue
        current = (
            session.execute(
                select(table).where(table.c.allocation_id == row["allocation_id"]).with_for_update()
            )
            .mappings()
            .one()
        )
        if current["refund_minor"] + amount > current["payment_minor"]:
            conflict()
        session.execute(
            refunds.insert().values(
                event_id=new_id("ar"),
                refund_id=refund_id,
                allocation_id=row["allocation_id"],
                amount_minor=amount,
                **provenance(ctx, now),
            )
        )
        session.execute(
            update(table)
            .where(table.c.allocation_id == row["allocation_id"])
            .values(refund_minor=current["refund_minor"] + amount, updated_at=now)
        )
    for aid in sorted({r["activity_id"] for r in rows}):
        record_actuals(session, ctx, aid, now)


def record_actuals(session, ctx, activity_id, now):
    act = (
        session.execute(
            select(activities)
            .where(*scope(activities, ctx), activities.c.activity_id == activity_id)
            .with_for_update()
        )
        .mappings()
        .one()
    )
    rows = (
        session.execute(select(allocations).where(allocations.c.activity_id == activity_id))
        .mappings()
        .all()
    )
    paid = [r for r in rows if r["status"] == "consumed"]
    revision = act["actual_revision"] + 1
    watermark = max(now, act["watermark"])
    payload = dict(
        contract_version=VERSION,
        store_id=ctx.store_id,
        snapshot_id=act["snapshot_id"],
        activity_id=activity_id,
        revision=revision,
        source_kind=act["manifest"]["source_kind"],
        watermark=watermark.isoformat(),
        window_start=act["manifest"]["start_at"],
        window_end=act["manifest"]["end_at"],
        bucket="activity_cumulative",
        payment_minor=str(sum(r["payment_minor"] for r in paid)),
        refund_minor=str(sum(r["refund_minor"] for r in paid)),
        consumed_discount_minor=str(act["consumed_discount_minor"]),
        reserved_discount_minor=str(act["reserved_discount_minor"]),
        paid_units=sum(r["qty"] for r in paid),
    )
    session.execute(
        update(activities)
        .where(activities.c.activity_id == activity_id)
        .values(actual_revision=revision, watermark=watermark, updated_at=now)
    )
    enqueue(session, act, "actuals", payload, now)
    return payload
