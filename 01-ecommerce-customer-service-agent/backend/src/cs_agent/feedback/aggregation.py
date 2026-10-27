from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from cs_agent.actions.repository import fingerprint, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.conversation.service import owned
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError
from cs_agent.db.experience_tables import feedback, snapshots
from cs_agent.db.runtime_tables import conversations, messages, runs
from cs_agent.identity.policies import require_role
from cs_agent.privacy.redaction import external_text
from cs_agent.wallet.service import event

CONTRACT = "XCS-SUPPORT-v0.1"
TOPICS = {
    "general",
    "order",
    "aftersale",
    "transport",
    "complaint",
    "knowledge_gap",
    "tool_failure",
}
K = 5
BUSINESS_ZONE = ZoneInfo("Asia/Shanghai")


def weekly_window(start):
    if start.tzinfo is None:
        raise AppError("invalid_input", 422, "时间窗须带时区")
    start = start.astimezone(BUSINESS_ZONE)
    if start.weekday() != 0 or any((start.hour, start.minute, start.second, start.microsecond)):
        raise AppError("invalid_input", 422, "聚合只提供上海时区周一零点起的固定周窗")
    return start, start + timedelta(days=7)


def suppress(records):
    """按distinct来源去重，按distinct主体保护；不导出主体集合和小样本分母。"""
    groups = defaultdict(dict)
    for source in records:
        for topic in source["topics"]:
            groups[(source["family"], topic)][source.get("unit", source["id"])] = source["owner"]
    results = []
    for family in ("consultations", "feedback", "tickets", "human_handoffs", "tool_failures"):
        family_groups = [(key, values) for key, values in groups.items() if key[0] == family]
        cells = []
        has_small = False
        for (_, topic), values in sorted(family_groups):
            released = len(values) >= K and len(set(values.values())) >= K
            has_small |= bool(values) and not released
            cells.append(
                {
                    "topic": topic,
                    "count": len(values) if released else None,
                    "suppressed": not released,
                }
            )
        # 同一族有小样本时再隐藏一个已满足阈值的单元，阻止互补差分。
        if has_small:
            visible = [cell for cell in cells if not cell["suppressed"]]
            if visible:
                additional = min(visible, key=lambda cell: (cell["count"], cell["topic"]))
                additional.update(count=None, suppressed=True)
        results.append(
            {
                "family": family,
                "cells": cells,
                "denominator": None,
                "denominator_definition": "distinct source units; multi-topic cells overlap",
                "total_suppressed": True,
            }
        )
    return results


class FeedbackService:
    def receive(self, session, principal, conversation_id, payload, now):
        owned(session, principal, conversation_id, True)
        row = (
            session.execute(
                select(messages).where(
                    messages.c.message_id == payload["message_id"],
                    messages.c.conversation_id == conversation_id,
                    messages.c.source_role.in_(["assistant", "support"]),
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise AppError("not_found", 404, "资源不存在或不可访问")
        topics = sorted(set(payload["topics"]))
        if not topics or set(topics) - TOPICS or payload["rating"] not in {"helpful", "unhelpful"}:
            raise AppError("invalid_input", 422, "反馈类型不符合契约")
        comment = external_text(payload.get("comment", ""))
        digest = fingerprint({"topics": topics, "rating": payload["rating"], "comment": comment})
        prior = (
            session.execute(
                select(feedback).where(
                    feedback.c.conversation_id == conversation_id,
                    feedback.c.message_id == row["message_id"],
                    feedback.c.owner_id == principal.owner_id,
                )
            )
            .mappings()
            .first()
        )
        if prior:
            if prior["content_digest"] != digest:
                raise AppError("conflict", 409, "反馈已经提交，不能用重试更换内容")
            return {"feedback_id": prior["feedback_id"], "status": "recorded"}
        ref = new_id("feedback")
        session.execute(
            feedback.insert().values(
                feedback_id=ref,
                conversation_id=conversation_id,
                message_id=row["message_id"],
                owner_id=principal.owner_id,
                rating=payload["rating"],
                topics=topics,
                comment=comment,
                content_digest=digest,
                store_id=principal.store_id,
                data_batch_id=principal.data_batch_id,
                batch_generation=principal.batch_generation,
                source_kind="derived",
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        return {"feedback_id": ref, "status": "recorded"}

    def _records(self, session, ctx, start, end):
        from cs_agent.db.support_tables import history, tickets

        def common(table):
            return (
                table.c.store_id == ctx.store_id,
                table.c.data_batch_id == ctx.data_batch_id,
                table.c.batch_generation == ctx.batch_generation,
                table.c.created_at >= start,
                table.c.created_at < end,
            )

        records = []
        for row in session.execute(select(feedback).where(*common(feedback))).mappings():
            records.append(
                {
                    "id": "feedback:" + row["feedback_id"],
                    "owner": row["owner_id"],
                    "family": "feedback",
                    "topics": row["topics"],
                    "digest": row["content_digest"],
                }
            )
        for row in session.execute(
            select(messages.c.message_id, messages.c.content_digest, conversations.c.owner_id)
            .join(conversations, conversations.c.conversation_id == messages.c.conversation_id)
            .where(*common(messages), messages.c.source_role == "customer")
        ).mappings():
            records.append(
                {
                    "id": "message:" + row["message_id"],
                    "owner": row["owner_id"],
                    "family": "consultations",
                    "topics": ["unclassified"],
                    "digest": row["content_digest"],
                }
            )
        for row in session.execute(select(tickets).where(*common(tickets))).mappings():
            records.append(
                {
                    "id": "ticket:" + row["ticket_id"],
                    "owner": row["owner_id"],
                    "family": "tickets",
                    "topics": [row["category"]],
                    "digest": fingerprint({"status": row["status"], "version": row["version"]}),
                }
            )
        for row in session.execute(
            select(runs).where(*common(runs), runs.c.error_code.is_not(None))
        ).mappings():
            records.append(
                {
                    "id": "run:" + row["run_id"],
                    "owner": row["owner_id"],
                    "family": "tool_failures",
                    "topics": ["tool_failure"],
                    "digest": fingerprint({"error": row["error_code"]}),
                }
            )
        for row in session.execute(
            select(history.c.history_id, tickets.c.owner_id, tickets.c.conversation_id)
            .join(tickets, tickets.c.ticket_id == history.c.ticket_id)
            .where(*common(history), history.c.action == "human_started")
        ).mappings():
            records.append(
                {
                    "id": "handoff:" + row["history_id"],
                    "unit": "conversation:" + row["conversation_id"],
                    "owner": row["owner_id"],
                    "family": "human_handoffs",
                    "topics": ["human"],
                    "digest": "takeover",
                }
            )
        return records

    def build(self, session, ctx, start, now):
        require_role(ctx, Role.SUPERVISOR)
        start, end = weekly_window(start)
        if end > now:
            raise AppError("state_conflict", 409, "统计周尚未结束，请使用已结束周窗")
        if end <= now - timedelta(days=180):
            raise AppError("history_expired", 410, "聚合窗口已到期，不能重新生成旧快照")
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
            {"scope": f"support-summary:{ctx.store_id}:{start.isoformat()}"},
        )
        records = self._records(session, ctx, start, end)
        mapping = {row["id"]: {"owner": row["owner"], "digest": row["digest"]} for row in records}
        digest = fingerprint(mapping)
        old = (
            session.execute(
                select(snapshots)
                .where(
                    snapshots.c.store_id == ctx.store_id,
                    snapshots.c.data_batch_id == ctx.data_batch_id,
                    snapshots.c.batch_generation == ctx.batch_generation,
                    snapshots.c.window_start == start,
                )
                .order_by(snapshots.c.revision.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        if old and old["source_digest"] == digest:
            return old["payload"]
        if old:
            changed = {
                value["owner"]
                for key, value in mapping.items()
                if old["source_ids"].get(key) != value
            }
            changed.update(
                value["owner"] for key, value in old["source_ids"].items() if key not in mapping
            )
            if len(changed) < K or now - old["created_at"] < timedelta(hours=24):
                return old["payload"]  # 不通过频繁修订暴露少量迟到记录。
        revision = old["revision"] + 1 if old else 1
        ref, event_id = new_id("snapshot_revision"), new_id("summary")
        stable_id = old["payload"]["snapshot_id"] if old else new_id("snapshot")
        counts = suppress(records)
        codes = {
            "consultations": "customer_message_count",
            "feedback": "feedback_count",
            "tickets": "ticket_count",
            "human_handoffs": "human_handoff_conversation_count",
            "tool_failures": "tool_failure_count",
        }
        metrics = [
            {
                "metric_code": codes[family["family"]],
                "dimension": {"topic_code": cell["topic"]},
                "count": cell["count"],
                "suppressed": cell["suppressed"],
                "unit": family["denominator_definition"],
                "denominator": None,
                "non_additive": True,
            }
            for family in counts
            for cell in family["cells"]
        ]
        from cs_agent.db.models import SourceBatch

        source = session.get(SourceBatch, ctx.data_batch_id)
        payload = {
            "contract_version": CONTRACT,
            "event_id": event_id,
            "snapshot_id": stable_id,
            "store_id": ctx.store_id,
            "revision": revision,
            "data_batch_id": ctx.data_batch_id,
            "batch_generation": ctx.batch_generation,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "timezone": "Asia/Shanghai",
            "generated_at": now.isoformat(),
            "source_kind": source.source_kind,
            "supersedes_revision": old["revision"] if old else None,
            "lineage_digest": digest,
            "dimensions": {
                "topic_dictionary_revision": "CS-TOPIC-v1",
                "product_category": "not_classified",
            },
            "metrics": metrics,
            "metric_revision": "CS-SUPPORT-METRICS-v1",
            "counts": counts,
            "privacy": {
                "min_subjects": K,
                "secondary_suppression": True,
                "raw_data_exported": False,
            },
            "completeness": {
                "status": "partial_classification",
                "non_additive_dimensions": ["topic_code"],
                "unclassified_consultations": True,
            },
            "classification_source": "customer_feedback_and_ticket_category; other_consultations_unclassified",
        }
        session.execute(
            snapshots.insert().values(
                snapshot_id=ref,
                window_start=start,
                window_end=end,
                revision=revision,
                source_digest=digest,
                source_ids=mapping,
                payload=payload,
                **provenance(ctx, now),
            )
        )
        event(
            session,
            ctx,
            "support_summary.corrected" if old else "support_summary.ready",
            stable_id,
            revision,
            ref,
            payload,
            now,
        )
        record(
            session,
            request_id=ctx.request_id,
            action="feedback.snapshot_created",
            outcome="succeeded",
            ctx=ctx,
            resource_ref=ref,
            summary={"version": revision},
        )
        return payload

    def read(self, session, store_id, start, now=None):
        from cs_agent.db.models import SourceBatch, Store

        store = session.get(Store, store_id)
        batch = session.get(SourceBatch, store.data_batch_id) if store else None
        if (
            not store
            or store.status != "active"
            or not batch
            or batch.status != "active"
            or store.batch_generation != batch.generation
        ):
            raise AppError("not_found", 404, "店铺或数据批次不可用")
        start, end = weekly_window(start)
        if end <= (now or datetime.now(UTC)) - timedelta(days=180):
            raise AppError("history_expired", 410, "聚合窗口已到期")
        row = session.execute(
            select(snapshots.c.payload)
            .where(
                snapshots.c.store_id == store_id,
                snapshots.c.window_start == start,
                snapshots.c.data_batch_id == store.data_batch_id,
                snapshots.c.batch_generation == batch.generation,
            )
            .order_by(snapshots.c.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            raise AppError("not_found", 404, "聚合快照尚未生成")
        return row
