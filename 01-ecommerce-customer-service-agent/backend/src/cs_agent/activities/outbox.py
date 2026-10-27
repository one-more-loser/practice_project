"""至少一次投递：短事务租约认领 → 事务外HTTP → 短事务ACK/重试。

每intent按数据库sequence保序；失败不删除，租约失效后可由另一进程恢复。
"""

from datetime import timedelta

from sqlalchemy import and_, exists, or_, select, update

from cs_agent.actions.repository import new_id
from cs_agent.activities.client import OperationsClient
from cs_agent.activities.repository import refresh_phase, retry_delay
from cs_agent.db.activity_tables import activities, deliveries


def claim(session, now, stores, lease_seconds=15):
    earlier = deliveries.alias("earlier")
    stmt = (
        select(deliveries)
        .where(
            deliveries.c.store_id.in_(stores),
            deliveries.c.status != "acked",
            deliveries.c.available_at <= now,
            or_(deliveries.c.status == "pending", deliveries.c.lease_until <= now),
            ~exists(
                select(earlier.c.event_id).where(
                    earlier.c.intent_id == deliveries.c.intent_id,
                    earlier.c.sequence < deliveries.c.sequence,
                    earlier.c.status != "acked",
                )
            ),
        )
        .order_by(deliveries.c.sequence)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    row = session.execute(stmt).mappings().first()
    if not row:
        return None
    token = new_id("lease")
    session.execute(
        update(deliveries)
        .where(deliveries.c.event_id == row["event_id"])
        .values(
            status="leased",
            attempts=row["attempts"] + 1,
            lease_token=token,
            lease_until=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )
    )
    return dict(row, lease_token=token, attempts=row["attempts"] + 1)


def finish(session, item, now, success, code):
    values = dict(
        status="acked" if success else "pending",
        lease_token=None,
        lease_until=None,
        acked_at=now if success else None,
        last_code=code,
        updated_at=now,
    )
    if not success:
        values["available_at"] = now + retry_delay(item["attempts"])
    return (
        session.execute(
            update(deliveries)
            .where(
                deliveries.c.event_id == item["event_id"],
                deliveries.c.status == "leased",
                deliveries.c.lease_token == item["lease_token"],
            )
            .values(**values)
        ).rowcount
        == 1
    )


def refresh_clock(db, clock, stores, limit=50):
    # 时钟变化是事实事件，即使没有顾客请求也持久投影并回传。
    with db.transaction() as session:
        now = clock.now()
        rows = (
            session.execute(
                select(activities)
                .where(
                    activities.c.store_id.in_(stores),
                    or_(
                        and_(activities.c.phase != "ended", activities.c.end_at <= now),
                        and_(activities.c.phase == "scheduled", activities.c.start_at <= now),
                        and_(
                            activities.c.phase == "ended",
                            activities.c.watermark < activities.c.end_at,
                            activities.c.end_at <= now,
                        ),
                    ),
                )
                .order_by(activities.c.activity_id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            .mappings()
            .all()
        )
        for row in rows:
            if row["phase"] == "ended":
                from types import SimpleNamespace

                from cs_agent.activities.pricing import record_actuals

                ctx = SimpleNamespace(
                    store_id=row["store_id"],
                    data_batch_id=row["data_batch_id"],
                    batch_generation=row["batch_generation"],
                )
                record_actuals(session, ctx, row["activity_id"], now)
            else:
                refresh_phase(session, row, now)
    return len(rows)


def deliver_once(db, settings, clock, *, client_factory=OperationsClient):
    stores = getattr(settings, "activity_service_stores", ())
    if not stores:
        return dict(status="disabled")
    with db.transaction() as session:
        item = claim(session, clock.now(), stores)
    if item is None:
        return dict(status="idle")
    success = False
    code = "delivery_unavailable"
    try:
        client = client_factory(settings, item["store_id"])
        path = "/api/ops/v1/internal/" + (
            "execution-receipts" if item["kind"] == "receipt" else "activity-actuals"
        )
        data = client.request("POST", path, body=item["payload"])
        # 非对象/非2xx已在client拒绝；consumer返回data对象表示业务事务已提交。
        success = isinstance(data, dict)
        code = "consumer_ack" if success else "consumer_contract"
    except Exception:
        # 不持久化异常正文/URL/凭据；进程死亡则原租约到期可恢复。
        code = "delivery_unavailable"
    with db.transaction() as session:
        accepted = finish(session, item, clock.now(), success, code)
    return dict(
        status="acked" if success and accepted else "retry",
        event_id=item["event_id"],
        attempts=item["attempts"],
    )


def tick(db, settings, clock, *, limit=10, client_factory=OperationsClient):
    stores = getattr(settings, "activity_service_stores", ())
    refresh_clock(db, clock, stores)
    results = []
    for _ in range(limit):
        result = deliver_once(db, settings, clock, client_factory=client_factory)
        results.append(result)
        if result["status"] in {"idle", "disabled", "retry"}:
            break
    return results
