from sqlalchemy import select, update

from cs_agent.actions.repository import fingerprint, new_id
from cs_agent.conversation.identity import common
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.runtime_tables import conversations, events, messages, runs, tasks
from cs_agent.privacy.redaction import external_text


def conflict():
    return AppError("conflict", 409, "会话或请求已变化，请刷新后重试")


def owned(session, principal, ref, lock=False):
    query = select(conversations).where(
        conversations.c.conversation_id == ref,
        conversations.c.owner_id == principal.owner_id,
        conversations.c.principal_kind == principal.kind,
        conversations.c.store_id == principal.store_id,
        conversations.c.data_batch_id == principal.data_batch_id,
        conversations.c.batch_generation == principal.batch_generation,
    )
    if lock:
        query = query.with_for_update()
    row = session.execute(query).mappings().first()
    if row is None:
        raise not_found()
    return dict(row)


def emit(session, conversation, type_, data, now):
    # 调用者必须持会话行锁；seq分配与事件提交属于同一短事务。
    sequence = conversation["last_seq"] + 1
    conversation["last_seq"] = sequence
    session.execute(
        update(conversations)
        .where(conversations.c.conversation_id == conversation["conversation_id"])
        .values(last_seq=sequence)
    )
    item = {
        "event_id": new_id("evt"),
        "conversation_id": conversation["conversation_id"],
        "stream_id": "public_" + conversation["conversation_id"],
        "seq": sequence,
        "mode_epoch": conversation["mode_epoch"],
        "type": type_,
        "data": data,
    }
    session.execute(
        events.insert().values(
            **item,
            store_id=conversation["store_id"],
            data_batch_id=conversation["data_batch_id"],
            batch_generation=conversation["batch_generation"],
            source_kind="derived",
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    return item


class ConversationService:
    def create(self, session, principal, now):
        ref = new_id("conv")
        session.execute(
            conversations.insert().values(
                conversation_id=ref,
                owner_id=principal.owner_id,
                principal_kind=principal.kind,
                status="open",
                mode="agent",
                mode_epoch=1,
                last_seq=0,
                floor_seq=0,
                next_message_seq=1,
                dialog_state={},
                **common(principal, now),
            )
        )
        return self.get(session, principal, ref)

    def get(self, session, principal, ref):
        row = owned(session, principal, ref)
        result = {
            key: row[key]
            for key in (
                "conversation_id",
                "status",
                "mode",
                "mode_epoch",
                "last_seq",
                "floor_seq",
                "version",
            )
        }
        # 增量字段，旧客户端仍读取原字段；不暴露后端sql_binding和身份指纹。
        state = row.get("dialog_state") or {}
        if state.get("sql_clarification"):
            from cs_agent.query.bindings import clean_clarification, scope_key

            ctx = principal.context
            if ctx and state.get("sql_binding",{}).get("scope") == scope_key(ctx):
                result["clarification"] = clean_clarification(state["sql_clarification"])
        if state.get("sql_response"):
            from cs_agent.query.bindings import clean_clarification

            result["query_response"] = clean_clarification(state["sql_response"])
        return result

    def accept_message(self, session, principal, ref, client_id, text, now):
        body = external_text(text).strip()
        if not body:
            raise AppError("invalid_input", 422, "请填写咨询内容")
        row = owned(session, principal, ref, True)
        digest = fingerprint({"text": body})
        prior = (
            session.execute(
                select(messages).where(
                    messages.c.conversation_id == ref,
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
        if row["status"] != "open":
            raise conflict()
        if row["mode"] in {"queued", "human"}:
            from cs_agent.support.human_service import accept_human_message

            return accept_human_message(session, principal, row, client_id, body, now)
        if row["mode"] != "agent":
            raise conflict()
        if row["active_run_id"]:
            previous = row["active_run_id"]
            old_run = (
                session.execute(select(runs).where(runs.c.run_id == previous)).mappings().first()
            )
            session.execute(
                update(runs)
                .where(
                    runs.c.run_id == previous, runs.c.status.in_(["queued", "running", "waiting"])
                )
                .values(status="superseded")
            )
            session.execute(
                update(tasks)
                .where(tasks.c.run_id == previous, tasks.c.status.in_(["queued", "leased"]))
                .values(status="cancelled", fence=tasks.c.fence + 1)
            )
            if old_run and old_run["status"] in {"queued", "running", "waiting"}:
                emit(session, row, "run.superseded", {"run_id": previous}, now)
        message, run = new_id("msg"), new_id("run")
        message_seq = row["next_message_seq"]
        row["next_message_seq"] += 1
        session.execute(
            update(conversations)
            .where(conversations.c.conversation_id == ref)
            .values(next_message_seq=row["next_message_seq"])
        )
        base = common(principal, now)
        session.execute(
            messages.insert().values(
                message_id=message,
                conversation_id=ref,
                source_role="customer",
                message_seq=message_seq,
                text=body,
                client_message_id=client_id,
                content_digest=digest,
                run_id=run,
                status="final",
                **base,
            )
        )
        session.execute(
            runs.insert().values(
                run_id=run,
                conversation_id=ref,
                message_id=message,
                owner_id=principal.owner_id,
                session_ref=principal.session_ref,
                auth_version=principal.auth_version,
                principal_kind=principal.kind,
                context_kind="conversation",
                mode_epoch=row["mode_epoch"],
                checkpoint_thread=run,
                status="queued",
                graph_version="CS-GRAPH-B2-v1",
                **base,
            )
        )
        session.execute(
            tasks.insert().values(
                task_id=new_id("task"), run_id=run, status="queued", fence=0, attempts=0, **base
            )
        )
        session.execute(
            update(conversations)
            .where(conversations.c.conversation_id == ref)
            .values(active_run_id=run, version=conversations.c.version + 1)
        )
        emit(session, row, "message.accepted", {"message_id": message, "run_id": run}, now)
        return {"message_id": message, "run_id": run, "accepted": True}

    def history(self, session, principal, ref, limit=20):
        owned(session, principal, ref)
        rows = (
            session.execute(
                select(messages)
                .where(messages.c.conversation_id == ref)
                .order_by(messages.c.message_seq.desc())
                .limit(limit)
            )
            .mappings()
            .all()
        )
        return [
            {
                key: row[key]
                for key in (
                    "message_id",
                    "source_role",
                    "text",
                    "citations",
                    "run_id",
                    "status",
                    "created_at",
                )
            }
            for row in reversed(rows)
        ]

    def replay(self, session, principal, ref, after, limit=50):
        row = owned(session, principal, ref)
        if after < row["floor_seq"]:
            raise AppError("history_reset_required", 409, "请重新读取会话历史")
        if after < 0 or after > row["last_seq"]:
            raise conflict()
        rows = session.execute(
            select(events)
            .where(events.c.conversation_id == ref, events.c.seq > after)
            .order_by(events.c.seq)
            .limit(limit)
        ).mappings()
        return [
            {
                "contract_version": "EVT-CS-v0.1",
                **{
                    key: event[key]
                    for key in (
                        "event_id",
                        "conversation_id",
                        "stream_id",
                        "seq",
                        "mode_epoch",
                        "type",
                        "data",
                    )
                },
                "occurred_at": event["created_at"].isoformat(),
            }
            for event in rows
        ]

    def run(self, session, principal, run_id, lock=False):
        row = session.execute(select(runs).where(runs.c.run_id == run_id)).mappings().first()
        if row is None:
            raise not_found()
        conversation = owned(session, principal, row["conversation_id"], lock)
        return dict(row), conversation

    def cancel(self, session, principal, run_id, now):
        run, conversation = self.run(session, principal, run_id, True)
        if run["status"] in {"queued", "running", "waiting"}:
            session.execute(update(runs).where(runs.c.run_id == run_id).values(status="cancelled"))
            session.execute(
                update(tasks)
                .where(tasks.c.run_id == run_id)
                .values(status="cancelled", fence=tasks.c.fence + 1)
            )
            emit(session, conversation, "run.cancelled", {"run_id": run_id}, now)
        return {
            "run_id": run_id,
            "status": "cancelled"
            if run["status"] in {"queued", "running", "waiting"}
            else run["status"],
            "business_effects": "preserved",
        }

    def close(self, session, principal, ref, expected, now):
        row = owned(session, principal, ref, True)
        if row["version"] != expected:
            raise conflict()
        if row["active_run_id"]:
            self.cancel(session, principal, row["active_run_id"], now)
        session.execute(
            update(conversations)
            .where(conversations.c.conversation_id == ref)
            .values(status="closed", version=conversations.c.version + 1)
        )
        return {"conversation_id": ref, "status": "closed"}
