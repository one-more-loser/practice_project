from datetime import timedelta

from sqlalchemy import select, update

from cs_agent.actions.repository import new_id
from cs_agent.activities.contracts import VERSION
from cs_agent.db.activity_tables import activities, deliveries, slots


def scope(table, ctx):
    return (
        table.c.store_id == ctx.store_id,
        table.c.data_batch_id == ctx.data_batch_id,
        table.c.batch_generation == ctx.batch_generation,
    )


def prov(row, now):
    return dict(
        store_id=row["store_id"],
        data_batch_id=row["data_batch_id"],
        batch_generation=row["batch_generation"],
        source_kind=row["source_kind"],
        created_at=now,
        updated_at=now,
        version=1,
    )


def enqueue(session, row, kind, payload, now):
    eid = payload.get("event_id") or new_id("evt")
    session.execute(
        deliveries.insert().values(
            event_id=eid,
            activity_id=payload.get("activity_id"),
            intent_id=row["intent_id"],
            kind=kind,
            payload=payload,
            status="pending",
            attempts=0,
            available_at=now,
            **prov(row, now),
        )
    )


def receipt(session, slot, now, activity=None, *, enqueue_event=False):
    if slot["status"] == "pending":
        return dict(status="unknown", intent_id=slot["intent_id"])
    if activity is None and slot["activity_id"]:
        activity = (
            session.execute(
                select(activities).where(activities.c.activity_id == slot["activity_id"])
            )
            .mappings()
            .one()
        )
    old = slot["receipt"] or {}
    # 同一修订重查保持事件id；新phase产生新事件但终态修订始终1。
    same = old and old.get("activity_revision") == (activity["revision"] if activity else None)
    payload = dict(
        contract_version=VERSION,
        event_id=old["event_id"] if same else new_id("evt"),
        store_id=slot["store_id"],
        intent_id=slot["intent_id"],
        approval_ref=slot["approval_ref"],
        manifest_digest=slot["manifest_digest"],
        status=slot["status"],
        terminal_revision=1,
        activity_id=activity["activity_id"] if activity else None,
        activity_revision=activity["revision"] if activity else None,
        phase=activity["phase"] if activity else None,
    )
    if enqueue_event:
        session.execute(
            update(slots)
            .where(slots.c.intent_id == slot["intent_id"])
            .values(receipt=payload, updated_at=now)
        )
        enqueue(session, slot, "receipt", payload, now)
    return payload


def refresh_phase(session, activity, now):
    phase = activity["phase"]
    target = (
        "ended"
        if now >= activity["end_at"]
        else ("active" if phase == "scheduled" and now >= activity["start_at"] else phase)
    )
    if target != phase:
        changed = dict(activity, phase=target, revision=activity["revision"] + 1, updated_at=now)
        session.execute(
            update(activities)
            .where(activities.c.activity_id == activity["activity_id"])
            .values(phase=target, revision=changed["revision"], updated_at=now)
        )
        slot = (
            session.execute(select(slots).where(slots.c.intent_id == activity["intent_id"]))
            .mappings()
            .one()
        )
        receipt(session, slot, now, changed, enqueue_event=True)
        if target == "ended":
            from types import SimpleNamespace

            from cs_agent.activities.pricing import record_actuals

            ctx = SimpleNamespace(
                store_id=activity["store_id"],
                data_batch_id=activity["data_batch_id"],
                batch_generation=activity["batch_generation"],
            )
            record_actuals(session, ctx, activity["activity_id"], now)
        return changed
    return activity


def retry_delay(attempts):
    return timedelta(seconds=min(300, 2 ** min(attempts, 8)))
