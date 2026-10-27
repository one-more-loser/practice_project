from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, or_, select, update

from cs_agent.actions.repository import fingerprint, new_id
from cs_agent.conversation.identity import Principal, common
from cs_agent.conversation.service import emit
from cs_agent.core.context import Role, TrustedContext
from cs_agent.core.errors import AppError
from cs_agent.db.models import LoginSession, SourceBatch, Store, Subject
from cs_agent.db.runtime_tables import conversations, guest_sessions, messages, runs, tasks
from cs_agent.identity.service import check_subject, roles_for
from cs_agent.privacy.redaction import external_text


@dataclass(frozen=True)
class Lease:
    task_id: str
    run_id: str
    conversation_id: str
    fence: int
    epoch: int


class RuntimeService:
    def claim(self, db, executor, now):
        with db.transaction() as session:
            conversation = (
                session.execute(
                    select(conversations)
                    .join(runs, runs.c.run_id == conversations.c.active_run_id)
                    .join(tasks, tasks.c.run_id == runs.c.run_id)
                    .where(
                        conversations.c.status == "open",
                        conversations.c.mode == "agent",
                        runs.c.status.in_(["queued", "running"]),
                        or_(
                            and_(
                                tasks.c.status == "queued",
                                or_(tasks.c.lease_until.is_(None), tasks.c.lease_until <= now),
                            ),
                            and_(tasks.c.status == "leased", tasks.c.lease_until <= now),
                        ),
                    )
                    .order_by(tasks.c.created_at)
                    .with_for_update(of=conversations, skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if not conversation:
                return None
            conversation = dict(conversation)
            task = (
                session.execute(
                    select(tasks)
                    .where(tasks.c.run_id == conversation["active_run_id"])
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if task["attempts"] >= 3:
                session.execute(
                    update(tasks)
                    .where(tasks.c.task_id == task["task_id"])
                    .values(status="failed", fence=tasks.c.fence + 1)
                )
                session.execute(
                    update(runs)
                    .where(runs.c.run_id == task["run_id"])
                    .values(status="failed", error_code="retry_budget_exceeded")
                )
                emit(
                    session,
                    conversation,
                    "run.failed",
                    {"run_id": task["run_id"], "code": "retry_budget_exceeded"},
                    now,
                )
                return None
            fence = task["fence"] + 1
            session.execute(
                update(tasks)
                .where(tasks.c.task_id == task["task_id"])
                .values(
                    status="leased",
                    fence=fence,
                    lease_until=now + timedelta(seconds=30),
                    executor_id=executor,
                    attempts=tasks.c.attempts + 1,
                )
            )
            session.execute(
                update(runs).where(runs.c.run_id == task["run_id"]).values(status="running")
            )
            emit(session, conversation, "run.started", {"run_id": task["run_id"]}, now)
            return Lease(
                task["task_id"],
                task["run_id"],
                conversation["conversation_id"],
                fence,
                conversation["mode_epoch"],
            )

    def verify(self, session, lease, now):
        conversation = (
            session.execute(
                select(conversations)
                .where(conversations.c.conversation_id == lease.conversation_id)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        task = (
            session.execute(select(tasks).where(tasks.c.task_id == lease.task_id).with_for_update())
            .mappings()
            .first()
        )
        run = session.execute(select(runs).where(runs.c.run_id == lease.run_id)).mappings().first()
        if (
            not conversation
            or not task
            or not run
            or task["fence"] != lease.fence
            or task["status"] != "leased"
            or task["lease_until"] <= now
            or conversation["status"] != "open"
            or conversation["mode"] != "agent"
            or conversation["mode_epoch"] != lease.epoch
            or conversation["active_run_id"] != lease.run_id
            or run["status"] != "running"
            or run["graph_version"] != "CS-GRAPH-B2-v1"
        ):
            raise AppError("stale_execution", 409, "任务已变更或停止")
        if run["principal_kind"] == "customer":
            current = session.get(LoginSession, run["session_ref"])
            subject = session.get(Subject, run["owner_id"])
            check_subject(session, subject)
            if (
                not current
                or current.status != "active"
                or current.expires_at <= now
                or current.active_role != "customer"
                or current.subject_id != run["owner_id"]
                or current.auth_version != run["auth_version"]
                or subject.auth_version != run["auth_version"]
                or Role.CUSTOMER not in roles_for(session, subject, now)
            ):
                raise AppError("unauthenticated", 401, "请重新登录")
            ctx = TrustedContext(
                subject.subject_id,
                subject.store_id,
                Role.CUSTOMER,
                current.session_id,
                subject.auth_version,
                subject.data_batch_id,
                subject.batch_generation,
                "req_" + lease.run_id,
            )
        else:
            guest = (
                session.execute(
                    select(guest_sessions).where(guest_sessions.c.guest_id == run["session_ref"])
                )
                .mappings()
                .first()
            )
            store = session.get(Store, run["store_id"])
            batch = session.get(SourceBatch, run["data_batch_id"])
            if (
                not guest
                or guest["status"] != "active"
                or guest["expires_at"] <= now
                or not store
                or store.status != "active"
                or not batch
                or batch.status != "active"
                or batch.generation != run["batch_generation"]
            ):
                raise AppError("unauthenticated", 401, "服务会话已失效")
            ctx = None
        principal = Principal(
            run["owner_id"],
            run["principal_kind"],
            run["store_id"],
            run["data_batch_id"],
            run["batch_generation"],
            run["session_ref"],
            run["auth_version"],
            ctx,
        )
        if ctx and (
            ctx.store_id != run["store_id"]
            or ctx.data_batch_id != run["data_batch_id"]
            or ctx.batch_generation != run["batch_generation"]
        ):
            raise AppError("unauthenticated", 401, "数据批次已变化")
        return dict(conversation), dict(run), principal

    @contextmanager
    def guard(self, db, lease, clock):
        with db.transaction() as session:
            yield session, self.verify(session, lease, clock.now())

    @staticmethod
    def _clarification_data(state, principal, waiting):
        from cs_agent.query.bindings import clean_clarification, scope_key

        ctx = principal.context
        if (waiting and ctx and state.get("sql_clarification")
                and state.get("sql_binding",{}).get("scope") == scope_key(ctx)):
            return {"clarification":clean_clarification(state["sql_clarification"])}
        if state.get("sql_response"):
            return {"query_response": clean_clarification(state["sql_response"])}
        return {}

    def finish(self, db, lease, clock, answer, state, *, waiting=False, error=None, citations=None):
        # 输出前再检查栅栏/身份；只在这里提交顾客可见回答与最终状态。
        body = external_text(answer)
        with self.guard(db, lease, clock) as (session, (conversation, run, principal)):
            now = clock.now()
            # 恢复已完成检查点也必须复核引用；知识失效不把旧答案重新发布。
            if citations:
                from cs_agent.knowledge.service import KnowledgeService

                knowledge = KnowledgeService()
                for citation in citations:
                    try:
                        if citation["kind"] == "faq":
                            knowledge.faq_reference(
                                session, principal, citation["reference_id"], now
                            )
                        elif citation["kind"] == "knowledge":
                            knowledge.reference(session, principal, citation["reference_id"], now)
                        else:
                            raise ValueError("unknown citation")
                    except (AppError, KeyError, ValueError):
                        raise AppError(
                            "evidence_changed", 409, "引用资料已失效，请重新查询"
                        ) from None
            message = new_id("msg")
            message_seq = conversation["next_message_seq"]
            conversation["next_message_seq"] += 1
            session.execute(
                messages.insert().values(
                    message_id=message,
                    conversation_id=lease.conversation_id,
                    source_role="assistant",
                    message_seq=message_seq,
                    text=body,
                    citations=citations or [],
                    client_message_id="reply_" + lease.run_id,
                    content_digest=fingerprint({"text": body}),
                    run_id=lease.run_id,
                    status="final",
                    **common(principal, now),
                )
            )
            session.execute(
                update(conversations)
                .where(conversations.c.conversation_id == lease.conversation_id)
                .values(next_message_seq=conversation["next_message_seq"])
            )
            status = "failed" if error else "waiting" if waiting else "completed"
            session.execute(
                update(runs)
                .where(runs.c.run_id == lease.run_id)
                .values(status=status, error_code=error)
            )
            session.execute(
                update(tasks).where(tasks.c.task_id == lease.task_id).values(status="done")
            )
            session.execute(
                update(conversations)
                .where(conversations.c.conversation_id == lease.conversation_id)
                .values(dialog_state=state)
            )
            emit(
                session,
                conversation,
                "answer.completed",
                {
                    "run_id": lease.run_id,
                    "message_id": message,
                    "text": body,
                    "citations": citations or [],
                    **self._clarification_data(state, principal, waiting),
                },
                now,
            )
            emit(
                session,
                conversation,
                "run.failed" if error else "run.completed",
                {"run_id": lease.run_id, "status": status, "code": error},
                now,
            )
            return message

    def fail(self, db, lease, clock, code, *, retryable=False):
        now = clock.now()
        with db.transaction() as session:
            conversation = (
                session.execute(
                    select(conversations)
                    .where(conversations.c.conversation_id == lease.conversation_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            task = (
                session.execute(
                    select(tasks).where(tasks.c.task_id == lease.task_id).with_for_update()
                )
                .mappings()
                .first()
            )
            run = (
                session.execute(select(runs).where(runs.c.run_id == lease.run_id).with_for_update())
                .mappings()
                .first()
            )
            if (
                not conversation
                or not task
                or not run
                or task["fence"] != lease.fence
                or task["status"] != "leased"
                or conversation["active_run_id"] != lease.run_id
                or conversation["mode_epoch"] != lease.epoch
                or conversation["status"] != "open"
            ):
                return False
            conversation = dict(conversation)
            if retryable and task["attempts"] < 3:
                delay = 2 ** max(0, task["attempts"] - 1)
                session.execute(
                    update(tasks)
                    .where(tasks.c.task_id == lease.task_id)
                    .values(
                        status="queued",
                        fence=tasks.c.fence + 1,
                        lease_until=now + timedelta(seconds=delay),
                        executor_id=None,
                    )
                )
                session.execute(
                    update(runs)
                    .where(runs.c.run_id == lease.run_id)
                    .values(status="queued", error_code=code)
                )
                emit(
                    session,
                    conversation,
                    "run.retry_scheduled",
                    {"run_id": lease.run_id, "code": code, "delay_seconds": delay},
                    now,
                )
                return True
            session.execute(
                update(tasks)
                .where(tasks.c.task_id == lease.task_id)
                .values(status="failed", fence=tasks.c.fence + 1, executor_id=None)
            )
            session.execute(
                update(runs)
                .where(runs.c.run_id == lease.run_id)
                .values(status="failed", error_code=code)
            )
            emit(
                session,
                conversation,
                "run.failed",
                {"run_id": lease.run_id, "status": "failed", "code": code},
                now,
            )
            return True
