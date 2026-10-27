from datetime import timedelta

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from cs_agent.audit.service import record
from cs_agent.config import Settings
from cs_agent.db.business_tables import outbox
from cs_agent.db.experience_tables import feedback, snapshots
from cs_agent.db.models import AuditEvent
from cs_agent.db.runtime_tables import conversations, events, messages, runs, tasks
from cs_agent.db.session import verify_target
from cs_agent.db.support_tables import entries, history

EXPIRED = "[会话内容已到期]"


def require_maintenance(engine):
    if engine.url.database not in {"cs_agent_b0", "cs_agent_b0_test"}:
        raise RuntimeError("maintenance target is outside this project")
    verify_target(engine, Settings(expected_database=engine.url.database), migration=True)


def clear_checkpoints(session, threads):
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        if session.scalar(text("SELECT to_regclass(:name)"), {"name": "lg." + table}):
            for offset in range(0, len(threads), 200):
                session.execute(
                    text(f"DELETE FROM lg.{table} WHERE thread_id=ANY(:threads)"),
                    {"threads": threads[offset : offset + 200]},
                )


def expire(engine, now):
    """只清理内容和已到期聚合/审计；不改订单、收货资料、余额或流水。"""
    if now.tzinfo is None:
        raise ValueError("maintenance clock requires timezone")
    require_maintenance(engine)
    cutoff = now - timedelta(days=30)
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('cs-maintenance',0))"))
        affected = session.scalars(
            select(messages.c.conversation_id)
            .where(messages.c.created_at < cutoff, messages.c.text != EXPIRED)
            .distinct()
        ).all()
        for ref in sorted(affected):
            conv = (
                session.execute(
                    select(conversations)
                    .where(conversations.c.conversation_id == ref)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            active = select(runs.c.run_id).where(
                runs.c.conversation_id == ref, runs.c.status.in_(["queued", "running", "waiting"])
            )
            session.execute(
                update(tasks)
                .where(tasks.c.run_id.in_(active))
                .values(
                    status="cancelled", fence=tasks.c.fence + 1, lease_until=None, updated_at=now
                )
            )
            session.execute(
                update(runs)
                .where(runs.c.run_id.in_(active))
                .values(status="cancelled", error_code="retention_expired", updated_at=now)
            )
            session.execute(
                update(conversations)
                .where(conversations.c.conversation_id == ref)
                .values(
                    dialog_state={},
                    active_run_id=None,
                    mode_epoch=conv["mode_epoch"] + 1,
                    version=conv["version"] + 1,
                    updated_at=now,
                )
            )
            # 维持人工/排队模式，不用内容清理冒充人工结束；旧输出栅栏作废。
            floor = session.scalar(
                select(func.max(events.c.seq)).where(
                    events.c.conversation_id == ref, events.c.created_at < cutoff
                )
            )
            if floor is not None:
                session.execute(
                    update(conversations)
                    .where(conversations.c.conversation_id == ref)
                    .values(floor_seq=max(floor, conv["floor_seq"]))
                )
        count = session.execute(
            update(messages)
            .where(messages.c.created_at < cutoff, messages.c.text != EXPIRED)
            .values(text=EXPIRED, citations=[], updated_at=now)
        ).rowcount
        session.execute(delete(events).where(events.c.created_at < cutoff))
        old_threads = session.scalars(
            select(runs.c.checkpoint_thread)
            .join(messages, messages.c.message_id == runs.c.message_id)
            .where(or_(messages.c.created_at < cutoff, runs.c.error_code == "retention_expired"))
        ).all()
        clear_checkpoints(session, old_threads)  # 重跑继续清除可能迟到的旧检查点，不复活旧任务。
        session.execute(
            update(feedback)
            .where(feedback.c.created_at < cutoff)
            .values(comment="", updated_at=now)
        )
        session.execute(
            update(entries)
            .where(entries.c.created_at < cutoff)
            .values(text=EXPIRED, updated_at=now)
        )
        session.execute(
            update(history)
            .where(history.c.created_at < cutoff)
            .values(reason=EXPIRED, data={}, updated_at=now)
        )
        audit_count = session.execute(
            delete(AuditEvent).where(AuditEvent.occurred_at < now - timedelta(days=90))
        ).rowcount
        expired_ids = session.scalars(
            select(snapshots.c.payload["snapshot_id"].astext).where(
                snapshots.c.window_end < now - timedelta(days=180)
            )
        ).all()
        if expired_ids:
            session.execute(
                delete(outbox).where(
                    outbox.c.event_type.in_(["support_summary.ready", "support_summary.corrected"]),
                    outbox.c.payload["snapshot_id"].astext.in_(expired_ids),
                )
            )
        summary_count = session.execute(
            delete(snapshots).where(snapshots.c.window_end < now - timedelta(days=180))
        ).rowcount
        record(
            session,
            request_id="retention-maintenance",
            action="retention.expired",
            outcome="succeeded",
            summary={"reason_code": "retention_policy_applied"},
        )
        return {
            "message_contents_expired": count,
            "conversations_fenced": len(affected),
            "audit_rows_expired": audit_count,
            "summary_revisions_expired": summary_count,
            "checkpoint_threads_checked": len(old_threads),
            "financial_records_modified": False,
        }
