"""人工状态变更与消息接收。所有方法使用调用者短事务，不持锁等待网络。"""

from sqlalchemy import select, update

from cs_agent.actions.repository import fingerprint, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.commerce.checkout import check_version
from cs_agent.conversation.identity import common
from cs_agent.conversation.service import conflict, emit, owned
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.runtime_tables import conversations, events, messages, runs, tasks
from cs_agent.db.support_tables import human_sessions, tickets
from cs_agent.support.service import (
    clean,
    create_case_ticket,
    log_history,
    require_ticket_scope,
    scope,
)


def lock_conversation(session, ctx, ref):
    row = (
        session.execute(
            select(conversations)
            .where(conversations.c.conversation_id == ref, *scope(conversations, ctx))
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if not row:
        raise not_found()
    return dict(row)


def active_human(session, ref, lock=False):
    query = select(human_sessions).where(
        human_sessions.c.conversation_id == ref, human_sessions.c.status.in_(["queued", "human"])
    )
    if lock:
        query = query.with_for_update()
    row = session.execute(query).mappings().first()
    return dict(row) if row else None


def check_mode(row, mode, expected_epoch, expected_version=None):
    if row["status"] != "open" or row["mode"] != mode or row["mode_epoch"] != expected_epoch:
        raise conflict()
    if expected_version is not None:
        check_version(row, expected_version)


def change_mode(session, row, mode, now, *, reason):
    # 与RuntimeService.verify共用conversation行锁；已提交的业务不被撤销。
    refs = select(runs.c.run_id).where(
        runs.c.conversation_id == row["conversation_id"],
        runs.c.status.in_(["queued", "running", "waiting"]),
    )
    session.execute(
        update(tasks)
        .where(tasks.c.run_id.in_(refs), tasks.c.status.in_(["queued", "leased"]))
        .values(status="cancelled", fence=tasks.c.fence + 1, lease_until=None, updated_at=now)
    )
    session.execute(
        update(runs)
        .where(
            runs.c.conversation_id == row["conversation_id"],
            runs.c.status.in_(["queued", "running", "waiting"]),
        )
        .values(status="superseded", updated_at=now)
    )
    row.update(
        mode=mode,
        mode_epoch=row["mode_epoch"] + 1,
        version=row["version"] + 1,
        active_run_id=None,
        dialog_state={},
    )
    session.execute(
        update(conversations)
        .where(conversations.c.conversation_id == row["conversation_id"])
        .values(
            mode=mode,
            mode_epoch=row["mode_epoch"],
            version=row["version"],
            active_run_id=None,
            dialog_state={},
            updated_at=now,
        )
    )
    emit(
        session,
        row,
        "service.mode_changed",
        {
            "mode": mode,
            "mode_epoch": row["mode_epoch"],
            "reason": reason,
            "version": row["version"],
        },
        now,
    )


def requeue_on_reassignment(session, ctx, conv, ticket_id, now):
    current = active_human(session, conv["conversation_id"], True)
    if current and current["ticket_id"] == ticket_id and current["status"] == "human":
        change_mode(session, conv, "queued", now, reason="assignment_changed")
        session.execute(
            update(human_sessions)
            .where(human_sessions.c.human_session_id == current["human_session_id"])
            .values(
                status="queued",
                assignment_id=None,
                staff_id=None,
                started_at=None,
                version=human_sessions.c.version + 1,
                updated_at=now,
            )
        )


def accept_human_message(session, principal, conversation_row, client_id, text, now):
    """顾客queued/human输入：与普通接收相同去重键，无run或Agent任务。"""
    row = conversation_row
    if (
        row["owner_id"] != principal.owner_id
        or row["store_id"] != principal.store_id
        or row["data_batch_id"] != principal.data_batch_id
        or row["batch_generation"] != principal.batch_generation
    ):
        raise not_found()
    body = clean(text, 4000)
    if row["status"] != "open" or row["mode"] not in {"queued", "human"}:
        raise conflict()
    digest = fingerprint({"text": body})
    prior = (
        session.execute(
            select(messages).where(
                messages.c.conversation_id == row["conversation_id"],
                messages.c.source_role == "customer",
                messages.c.client_message_id == client_id,
            )
        )
        .mappings()
        .first()
    )
    if prior:
        if prior["content_digest"] != digest:
            raise conflict()
        return {"message_id": prior["message_id"], "run_id": prior["run_id"], "accepted": True}
    ref = new_id("msg")
    seq = row["next_message_seq"]
    session.execute(
        messages.insert().values(
            message_id=ref,
            conversation_id=row["conversation_id"],
            source_role="customer",
            message_seq=seq,
            text=body,
            client_message_id=client_id,
            content_digest=digest,
            run_id=None,
            status="final",
            **common(principal, now),
        )
    )
    row["next_message_seq"] += 1
    row["version"] += 1
    session.execute(
        update(conversations)
        .where(conversations.c.conversation_id == row["conversation_id"])
        .values(next_message_seq=row["next_message_seq"], version=row["version"], updated_at=now)
    )
    emit(
        session,
        row,
        "message.accepted",
        {
            "message_id": ref,
            "run_id": None,
            "message_seq": seq,
            "source_role": "customer",
            "text": body,
        },
        now,
    )
    return {"message_id": ref, "run_id": None, "accepted": True}


class HumanService:
    def request(self, session, principal, ref, reason, expected_epoch, expected_version, now):
        if principal.kind != "customer":
            raise AppError("forbidden", 403, "请登录后申请人工服务")
        reason = clean(reason)
        row = owned(session, principal, ref, True)
        check_mode(row, "agent", expected_epoch, expected_version)
        ctx = principal.context
        ticket_id = create_case_ticket(session, ctx, reason, "general", now)
        session.execute(
            update(tickets).where(tickets.c.ticket_id == ticket_id).values(conversation_id=ref)
        )
        session.execute(
            human_sessions.insert().values(
                human_session_id=new_id("human"),
                conversation_id=ref,
                ticket_id=ticket_id,
                status="queued",
                requested_at=now,
                **provenance(ctx, now),
            )
        )
        change_mode(session, row, "queued", now, reason="customer_requested")
        log_history(session, ctx, ticket_id, "human_requested", reason, now)
        return self.view(row, ticket_id, connected=False)

    def cancel_request(self, session, principal, ref, expected_epoch, expected_version, now):
        row = owned(session, principal, ref, True)
        check_mode(row, "queued", expected_epoch, expected_version)
        current = active_human(session, ref, True)
        if not current or current["status"] != "queued":
            raise conflict()
        session.execute(
            update(human_sessions)
            .where(human_sessions.c.human_session_id == current["human_session_id"])
            .values(
                status="cancelled",
                ended_at=now,
                end_reason="customer_cancelled",
                version=human_sessions.c.version + 1,
                updated_at=now,
            )
        )
        change_mode(session, row, "agent", now, reason="customer_cancelled_queue")
        log_history(
            session,
            principal.context,
            current["ticket_id"],
            "human_cancelled",
            "顾客取消人工等待，工单保留",
            now,
        )
        return self.view(row, current["ticket_id"], connected=False)

    def _assignment(self, session, ctx, current, assignment_id, assignment_version):
        if ctx.role != Role.SUPPORT:
            raise AppError("forbidden", 403, "请使用获分派客服身份处理人工服务")
        ticket = require_ticket_scope(
            session, ctx, current["ticket_id"], lock=True, allow_supervisor=False
        )
        if (
            ticket["assignment_id"] != assignment_id
            or ticket["assignment_version"] != assignment_version
        ):
            raise conflict()
        return ticket

    def takeover(
        self,
        session,
        ctx,
        ref,
        assignment_id,
        assignment_version,
        expected_epoch,
        expected_version,
        now,
    ):
        row = lock_conversation(session, ctx, ref)
        check_mode(row, "queued", expected_epoch, expected_version)
        current = active_human(session, ref)
        if not current:
            raise conflict()
        self._assignment(session, ctx, current, assignment_id, assignment_version)
        session.execute(
            update(human_sessions)
            .where(human_sessions.c.human_session_id == current["human_session_id"])
            .values(
                status="human",
                assignment_id=assignment_id,
                staff_id=ctx.subject_id,
                started_at=now,
                version=human_sessions.c.version + 1,
                updated_at=now,
            )
        )
        change_mode(session, row, "human", now, reason="staff_takeover")
        log_history(session, ctx, current["ticket_id"], "human_started", "获分派客服接管", now)
        return self.view(row, current["ticket_id"], connected=True)

    def end(
        self,
        session,
        ctx,
        ref,
        assignment_id,
        assignment_version,
        expected_epoch,
        expected_version,
        reason,
        now,
    ):
        reason = clean(reason)
        row = lock_conversation(session, ctx, ref)
        check_mode(row, "human", expected_epoch, expected_version)
        current = active_human(session, ref)
        if not current:
            raise conflict()
        self._assignment(session, ctx, current, assignment_id, assignment_version)
        if current["assignment_id"] != assignment_id or current["staff_id"] != ctx.subject_id:
            raise conflict()
        session.execute(
            update(human_sessions)
            .where(human_sessions.c.human_session_id == current["human_session_id"])
            .values(
                status="ended",
                ended_at=now,
                end_reason=reason,
                version=human_sessions.c.version + 1,
                updated_at=now,
            )
        )
        change_mode(session, row, "agent", now, reason="staff_ended")
        log_history(session, ctx, current["ticket_id"], "human_ended", reason, now)
        return self.view(row, current["ticket_id"], connected=False)

    @staticmethod
    def view(row, ticket_id, *, connected):
        return {
            "conversation_id": row["conversation_id"],
            "ticket_id": ticket_id,
            "mode": row["mode"],
            "mode_epoch": row["mode_epoch"],
            "version": row["version"],
            "connected": connected,
        }

    def authorized_conversation(self, session, identity, ref):
        if hasattr(identity, "owner_id"):
            return owned(session, identity, ref)
        row = (
            session.execute(
                select(conversations).where(
                    conversations.c.conversation_id == ref, *scope(conversations, identity)
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise not_found()
        current = active_human(session, ref)
        if current:
            require_ticket_scope(session, identity, current["ticket_id"])
        else:
            # 结束后仍有有效分派的工单可读历史；改派随时即时重新核验。
            refs = session.scalars(
                select(tickets.c.ticket_id)
                .where(tickets.c.conversation_id == ref, *scope(tickets, identity))
                .order_by(tickets.c.created_at.desc())
            ).all()
            for ticket_id in refs:
                try:
                    require_ticket_scope(session, identity, ticket_id)
                    return dict(row)
                except AppError:
                    continue
            raise not_found()
        return dict(row)

    def staff_reply(
        self, session, ctx, ref, client_id, text, assignment_version, expected_epoch, now
    ):
        body = clean(text, 4000)
        row = lock_conversation(session, ctx, ref)
        current = active_human(session, ref)
        if not current:
            raise conflict()
        self._assignment(session, ctx, current, current["assignment_id"], assignment_version)
        check_mode(row, "human", expected_epoch)
        if current["staff_id"] != ctx.subject_id:
            raise not_found()
        # 员工身份也进入client key，避免新负责人伪造旧员工的ACK。
        bound_client_id = "staff_" + fingerprint(
            {"actor_id": ctx.subject_id, "client_id": client_id}
        )
        digest = fingerprint({"text": body})
        prior = (
            session.execute(
                select(messages).where(
                    messages.c.conversation_id == ref,
                    messages.c.source_role == "support",
                    messages.c.client_message_id == bound_client_id,
                )
            )
            .mappings()
            .first()
        )
        if prior:
            if prior["content_digest"] != digest:
                raise conflict()
            return {
                "message_id": prior["message_id"],
                "message_seq": prior["message_seq"],
                "accepted": True,
            }
        seq, message_id = row["next_message_seq"], new_id("msg")
        session.execute(
            messages.insert().values(
                message_id=message_id,
                conversation_id=ref,
                source_role="support",
                message_seq=seq,
                text=body,
                client_message_id=bound_client_id,
                content_digest=digest,
                run_id=None,
                status="final",
                **provenance(ctx, now),
            )
        )
        session.execute(
            update(conversations)
            .where(conversations.c.conversation_id == ref)
            .values(next_message_seq=seq + 1, version=row["version"] + 1, updated_at=now)
        )
        ticket = require_ticket_scope(
            session, ctx, current["ticket_id"], lock=True, allow_supervisor=False
        )
        if ticket["first_reply_at"] is None:
            session.execute(
                update(tickets)
                .where(tickets.c.ticket_id == ticket["ticket_id"])
                .values(first_reply_at=now, updated_at=now, version=tickets.c.version + 1)
            )
        emit(
            session,
            row,
            "message.accepted",
            {
                "message_id": message_id,
                "message_seq": seq,
                "run_id": None,
                "source_role": "support",
                "text": body,
            },
            now,
        )
        record(
            session,
            request_id=ctx.request_id,
            action="human.message.sent",
            outcome="succeeded",
            ctx=ctx,
            resource_ref=message_id,
            summary={"version": seq},
        )
        return {"message_id": message_id, "message_seq": seq, "accepted": True}

    def history(self, session, identity, ref):
        row = self.authorized_conversation(session, identity, ref)
        items = [
            {
                key: item[key]
                for key in ("message_id", "source_role", "message_seq", "text", "created_at")
            }
            for item in session.execute(
                select(messages)
                .where(messages.c.conversation_id == ref)
                .order_by(messages.c.message_seq.desc())
                .limit(100)
            ).mappings()
        ]
        current = active_human(session, ref)
        return {
            **{
                key: row[key]
                for key in ("conversation_id", "mode", "mode_epoch", "version", "last_seq")
            },
            "ticket_id": current["ticket_id"] if current else None,
            "items": list(reversed(items)),
        }

    def replay(self, session, identity, ref, after, expected_epoch):
        row = self.authorized_conversation(session, identity, ref)
        if after < row["floor_seq"]:
            raise AppError("history_reset_required", 409, "请重新读取会话历史")
        if after < 0 or after > row["last_seq"]:
            raise conflict()
        # 模式变化优先；不把旧epoch输出继续投递给新模式。
        rows = (
            session.execute(
                select(events)
                .where(events.c.conversation_id == ref, events.c.seq > after)
                .order_by(events.c.seq)
                .limit(100)
            )
            .mappings()
            .all()
        )
        output = [
            {
                "contract_version": "EVT-CS-v0.1",
                **{
                    key: item[key]
                    for key in (
                        "event_id",
                        "stream_id",
                        "seq",
                        "conversation_id",
                        "mode_epoch",
                        "type",
                        "data",
                    )
                },
                "occurred_at": item["created_at"].isoformat(),
            }
            for item in rows
            if item["mode_epoch"] == row["mode_epoch"]
        ]
        # 跳过旧epoch事件也前移内部读取位置，防止在旧事件页上停滞。
        cursor = rows[-1]["seq"] if rows else after
        return row, output, cursor
