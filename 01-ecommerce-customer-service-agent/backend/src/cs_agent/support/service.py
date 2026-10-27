from datetime import timedelta

from sqlalchemy import select, update

from cs_agent.actions.repository import new_id, provenance
from cs_agent.audit.service import record
from cs_agent.commerce.checkout import check_version, owned_order
from cs_agent.conversation.identity import Principal
from cs_agent.conversation.service import emit, owned
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.models import Subject
from cs_agent.db.support_tables import assignments, entries, history, tickets
from cs_agent.identity.service import roles_for
from cs_agent.privacy.redaction import external_text

CATEGORIES = {"general", "order", "aftersale", "transport", "complaint"}
FINAL = {"resolved", "closed"}


def clean(value, limit=500):
    if not isinstance(value, str):
        raise AppError("invalid_input", 422, "请填写有效文字")
    body = external_text(value, max_length=limit).strip()
    if not body:
        raise AppError("invalid_input", 422, "请填写处理内容或原因")
    return body


def scope(table, ctx):
    return (
        table.c.store_id == ctx.store_id,
        table.c.data_batch_id == ctx.data_batch_id,
        table.c.batch_generation == ctx.batch_generation,
    )


def principal_from(ctx):
    return Principal(
        ctx.subject_id,
        "customer",
        ctx.store_id,
        ctx.data_batch_id,
        ctx.batch_generation,
        ctx.session_ref,
        ctx.auth_version,
        ctx,
    )


def ticket_row(session, ctx, ticket_id, *, lock=False):
    query = select(tickets).where(tickets.c.ticket_id == ticket_id, *scope(tickets, ctx))
    if lock:
        # 不修改工单主键；NO KEY UPDATE兼容新业务申请的外键KEY SHARE。
        query = query.with_for_update(key_share=True)
    row = session.execute(query).mappings().first()
    if not row:
        raise not_found()
    return dict(row)


def require_ticket_scope(session, ctx, ticket_id, *, lock=False, allow_supervisor=True):
    """唯一员工详情门槛；普通审核显式关闭主管例外能力。"""
    row = ticket_row(session, ctx, ticket_id, lock=lock)
    if ctx.role == Role.SUPERVISOR and allow_supervisor:
        return row
    if ctx.role != Role.SUPPORT or row["assigned_staff_id"] != ctx.subject_id:
        raise not_found()
    assignment = (
        session.execute(
            select(assignments).where(
                assignments.c.assignment_id == row["assignment_id"],
                assignments.c.ticket_id == ticket_id,
                assignments.c.staff_id == ctx.subject_id,
                assignments.c.status == "active",
                *scope(assignments, ctx),
            )
        )
        .mappings()
        .first()
    )
    if assignment is None:
        raise not_found()
    row["assignment_version"] = assignment["version"]
    return row


def assigned_ticket_ids(ctx):
    """在数据库分页前约束有效分派，避免扫描所有申请后逐条查权限。"""
    return (
        select(tickets.c.ticket_id)
        .join(assignments, assignments.c.assignment_id == tickets.c.assignment_id)
        .where(
            *scope(tickets, ctx),
            *scope(assignments, ctx),
            tickets.c.assigned_staff_id == ctx.subject_id,
            assignments.c.staff_id == ctx.subject_id,
            assignments.c.status == "active",
        )
    )


def log_history(session, ctx, ticket_id, action, reason, now, **data):
    session.execute(
        history.insert().values(
            history_id=new_id("th"),
            ticket_id=ticket_id,
            action=action,
            actor_id=ctx.subject_id,
            reason=reason,
            data=data,
            **provenance(ctx, now),
        )
    )
    record(
        session,
        request_id=ctx.request_id,
        action="ticket." + action,
        outcome="succeeded",
        ctx=ctx,
        resource_ref=ticket_id,
    )


def create_case_ticket(
    session, ctx, subject, category, now, *, order_id=None, case_kind=None, case_ref=None
):
    """调用者已完成确认/业务门槛；不自行提交或制造业务退款效果。"""
    if ctx.role != Role.CUSTOMER or category not in CATEGORIES:
        raise AppError("forbidden", 403, "当前身份不能创建此工单")
    subject = clean(subject, 160)
    if order_id:
        owned_order(session, ctx, order_id)
    ref = new_id("ticket")
    high = category == "complaint"
    session.execute(
        tickets.insert().values(
            ticket_id=ref,
            owner_id=ctx.subject_id,
            subject=subject,
            summary=subject,
            category=category,
            priority="high" if high else "normal",
            priority_reason="顾客投诉，交主管关注" if high else "普通服务诉求",
            status="open",
            order_id=order_id,
            case_kind=case_kind,
            case_ref=case_ref,
            sla_due_at=now + timedelta(minutes=10 if high else 60),
            **provenance(ctx, now),
        )
    )
    log_history(
        session, ctx, ref, "created", "顾客确认提交" if not case_kind else "业务申请关联工单", now
    )
    return ref


def _public(row, now):
    result = {
        name: row[name]
        for name in (
            "ticket_id",
            "subject",
            "summary",
            "category",
            "priority",
            "priority_reason",
            "status",
            "conversation_id",
            "order_id",
            "version",
            "assignment_id",
            "linked_from_ticket_id",
            "case_kind",
            "case_ref",
            "created_at",
            "first_reply_at",
            "sla_due_at",
            "closed_at",
            "resolved_at",
        )
    }
    result["sla_overdue"] = row["first_reply_at"] is None and now > row["sla_due_at"]
    return result


class TicketService:
    create_case_ticket = staticmethod(create_case_ticket)

    def prepare_action(self, session, ctx, action, resource_ref, parameters, expected_version, now):
        if ctx.role != Role.CUSTOMER or action != "ticket.create":
            raise AppError("forbidden", 403, "当前身份不能创建工单")
        if set(parameters) - {"subject", "category", "order_id", "description"}:
            raise AppError("invalid_input", 422, "工单参数不符合契约")
        category = parameters.get("category", "general")
        if category not in CATEGORIES:
            raise AppError("invalid_input", 422, "请选择有效工单类别")
        subject = clean(parameters.get("subject", ""), 160)
        description = clean(parameters.get("description", subject), 4000)
        conv = None
        if resource_ref != "new":
            conv = owned(session, principal_from(ctx), resource_ref)
            check_version(conv, expected_version)
            if conv["status"] != "open":
                raise AppError("state_conflict", 409, "会话已关闭")
        elif expected_version != 1:
            raise AppError("state_conflict", 409, "新工单版本不匹配")
        order_id = parameters.get("order_id")
        if order_id:
            owned_order(session, ctx, order_id)
        normalized = {
            "subject": subject,
            "description": description,
            "category": category,
            "order_id": order_id,
            "conversation_id": conv["conversation_id"] if conv else None,
        }
        return normalized, {
            "action": action,
            "subject": subject,
            "category": category,
            "order_id": order_id,
            "notice": "提交后进入店铺处理队列",
        }

    def apply_confirmed_action(self, session, ctx, confirmation_row, op_id, now):
        params = confirmation_row["parameters"]
        conv = None
        if params.get("conversation_id"):
            conv = owned(session, principal_from(ctx), params["conversation_id"], True)
            check_version(conv, confirmation_row["expected_version"])
            if conv["status"] != "open":
                raise AppError("state_conflict", 409, "会话已关闭")
        ref = create_case_ticket(
            session,
            ctx,
            params["subject"],
            params["category"],
            now,
            order_id=params.get("order_id"),
        )
        if conv:
            session.execute(
                update(tickets)
                .where(tickets.c.ticket_id == ref)
                .values(conversation_id=conv["conversation_id"])
            )
            emit(session, conv, "ticket.updated", {"ticket_id": ref, "status": "open"}, now)
        session.execute(
            entries.insert().values(
                entry_id=new_id("te"),
                ticket_id=ref,
                actor_id=ctx.subject_id,
                source_role="customer",
                visibility="public",
                text=params["description"],
                **provenance(ctx, now),
            )
        )
        return {"business_ref": ref, "ticket_id": ref, "ticket_status": "open"}

    def get(self, session, ctx, ticket_id, now, *, staff=False):
        if staff:
            row = require_ticket_scope(session, ctx, ticket_id)
        else:
            if ctx.role != Role.CUSTOMER:
                raise not_found()
            row = ticket_row(session, ctx, ticket_id)
            if row["owner_id"] != ctx.subject_id:
                raise not_found()
        result = _public(row, now)
        if staff:
            result.update(
                owner_id=row["owner_id"],
                assigned_staff_id=row["assigned_staff_id"],
                assignment_version=row.get("assignment_version", 1),
            )
            if row["order_id"]:
                from dataclasses import replace

                from cs_agent.commerce.query import OrderQuery

                # 收款人/订单主体只来自已分派工单，不接受员工传顾客身份。
                owner = replace(ctx, subject_id=row["owner_id"], role=Role.CUSTOMER)
                result["related_order"] = OrderQuery().get(
                    session, owner, row["order_id"], now, include_private=False
                )
        query = select(entries).where(entries.c.ticket_id == ticket_id)
        if not staff:
            query = query.where(entries.c.visibility == "public")
        result["entries"] = [
            {
                key: entry[key]
                for key in ("entry_id", "source_role", "visibility", "text", "created_at")
            }
            for entry in session.execute(
                query.order_by(entries.c.created_at, entries.c.entry_id)
            ).mappings()
        ]
        if staff:
            result["history"] = [
                {key: item[key] for key in ("action", "actor_id", "reason", "data", "created_at")}
                for item in session.execute(
                    select(history)
                    .where(history.c.ticket_id == ticket_id)
                    .order_by(history.c.created_at, history.c.history_id)
                ).mappings()
            ]
        return result

    def list_owned(self, session, ctx, now):
        if ctx.role != Role.CUSTOMER:
            raise not_found()
        return [
            _public(row, now)
            for row in session.execute(
                select(tickets)
                .where(tickets.c.owner_id == ctx.subject_id, *scope(tickets, ctx))
                .order_by(tickets.c.created_at.desc())
                .limit(100)
            ).mappings()
        ]

    def queue(self, session, ctx, now):
        if ctx.role not in {Role.SUPPORT, Role.SUPERVISOR}:
            raise AppError("forbidden", 403, "当前角色不能访问工作台")
        rows = session.execute(
            select(tickets)
            .where(*scope(tickets, ctx))
            .order_by(tickets.c.sla_due_at, tickets.c.ticket_id)
            .limit(100)
        ).mappings()
        return [
            {
                "ticket_id": row["ticket_id"],
                "summary": row["summary"],
                "category": row["category"],
                "priority": row["priority"],
                "status": row["status"],
                "version": row["version"],
                "created_at": row["created_at"],
                "sla_due_at": row["sla_due_at"],
                "sla_overdue": row["first_reply_at"] is None and now > row["sla_due_at"],
                "assigned_to_me": row["assigned_staff_id"] == ctx.subject_id,
            }
            for row in rows
        ]

    def assign(self, session, ctx, ticket_id, staff_id, reason, expected, now):
        if ctx.role != Role.SUPERVISOR:
            raise AppError("forbidden", 403, "仅主管可以分派")
        reason = clean(reason)
        initial = ticket_row(session, ctx, ticket_id)
        # 一旦关联会话，统一会话→工单锁顺序，避免改派与接管互相等待。
        conv = None
        if initial["conversation_id"]:
            from cs_agent.support.human_service import lock_conversation

            conv = lock_conversation(session, ctx, initial["conversation_id"])
        row = require_ticket_scope(session, ctx, ticket_id, lock=True)
        check_version(row, expected)
        if row["status"] in FINAL:
            raise AppError("state_conflict", 409, "请先重开工单")
        subject = session.get(Subject, staff_id)
        if (
            not subject
            or subject.store_id != ctx.store_id
            or subject.account_status != "active"
            or subject.data_batch_id != ctx.data_batch_id
            or subject.batch_generation != ctx.batch_generation
            or Role.SUPPORT not in roles_for(session, subject, now)
        ):
            raise AppError("invalid_input", 422, "负责人必须是本店有效客服")
        session.execute(
            update(assignments)
            .where(assignments.c.ticket_id == ticket_id, assignments.c.status == "active")
            .values(
                status="revoked", revoked_at=now, version=assignments.c.version + 1, updated_at=now
            )
        )
        ref = new_id("assign")
        session.execute(
            assignments.insert().values(
                assignment_id=ref,
                ticket_id=ticket_id,
                staff_id=staff_id,
                assigned_by=ctx.subject_id,
                reason=reason,
                status="active",
                **provenance(ctx, now),
            )
        )
        session.execute(
            update(tickets)
            .where(tickets.c.ticket_id == ticket_id)
            .values(
                assigned_staff_id=staff_id,
                assignment_id=ref,
                status="assigned",
                version=row["version"] + 1,
                updated_at=now,
            )
        )
        log_history(
            session,
            ctx,
            ticket_id,
            "assigned",
            reason,
            now,
            staff_id=staff_id,
            previous_staff_id=row["assigned_staff_id"],
        )
        if conv:
            from cs_agent.support.human_service import requeue_on_reassignment

            requeue_on_reassignment(session, ctx, conv, ticket_id, now)
        return {
            "ticket_id": ticket_id,
            "assignment_id": ref,
            "assignment_version": 1,
            "version": row["version"] + 1,
        }

    def act(self, session, ctx, ticket_id, action, text, expected, now):
        if action not in {"accept", "reply", "note", "wait", "resolve", "close", "escalate"}:
            raise AppError("invalid_input", 422, "工单动作不可用")
        body = clean(text, 4000) if action != "accept" else "客服接单"
        row = require_ticket_scope(session, ctx, ticket_id, lock=True)
        check_version(row, expected)
        if row["status"] in FINAL:
            raise AppError("state_conflict", 409, "当前工单已结束，请重开后处理")
        values = {"version": row["version"] + 1, "updated_at": now}
        if action == "accept":
            if (
                ctx.role != Role.SUPPORT
                or row["assigned_staff_id"] != ctx.subject_id
                or row["status"] != "assigned"
            ):
                raise AppError("state_conflict", 409, "仅获分派客服可接单")
            values["status"] = "in_progress"
        elif action in {"reply", "note", "resolve", "close"}:
            session.execute(
                entries.insert().values(
                    entry_id=new_id("te"),
                    ticket_id=ticket_id,
                    actor_id=ctx.subject_id,
                    source_role="support",
                    visibility="internal" if action == "note" else "public",
                    text=body,
                    **provenance(ctx, now),
                )
            )
            if action != "note" and row["first_reply_at"] is None:
                values["first_reply_at"] = now
            if action in {"resolve", "close"}:
                values["status"] = "resolved" if action == "resolve" else "closed"
                values["resolved_at" if action == "resolve" else "closed_at"] = now
        elif action == "wait":
            values["status"] = "waiting_customer"
        elif action == "escalate":
            values.update(
                priority="high", category="complaint", priority_reason=body, escalated_at=now
            )
            if row["first_reply_at"] is None:
                values["sla_due_at"] = min(
                    row["sla_due_at"], row["created_at"] + timedelta(minutes=10)
                )
        session.execute(update(tickets).where(tickets.c.ticket_id == ticket_id).values(**values))
        log_history(session, ctx, ticket_id, action, body[:500], now)
        return {
            "ticket_id": ticket_id,
            "status": values.get("status", row["status"]),
            "version": values["version"],
        }

    def reopen(self, session, ctx, ticket_id, reason, expected, now):
        if ctx.role != Role.CUSTOMER:
            raise not_found()
        row = ticket_row(session, ctx, ticket_id, lock=True)
        if row["owner_id"] != ctx.subject_id:
            raise not_found()
        check_version(row, expected)
        if row["status"] not in FINAL:
            raise AppError("state_conflict", 409, "工单仍在处理中")
        reason = clean(reason)
        ended = row["closed_at"] or row["resolved_at"]
        if ended is None:
            raise AppError("state_conflict", 409, "工单结束时间缺失，需人工核查")
        if now - ended > timedelta(days=7):
            ref = create_case_ticket(
                session, ctx, row["subject"], row["category"], now, order_id=row["order_id"]
            )
            session.execute(
                update(tickets)
                .where(tickets.c.ticket_id == ref)
                .values(linked_from_ticket_id=ticket_id)
            )
            log_history(session, ctx, ticket_id, "linked_new", reason, now, linked_ticket_id=ref)
            return {
                "ticket_id": ref,
                "linked_from_ticket_id": ticket_id,
                "status": "open",
                "version": 1,
            }
        session.execute(
            update(tickets)
            .where(tickets.c.ticket_id == ticket_id)
            .values(
                status="reopened",
                closed_at=None,
                resolved_at=None,
                version=row["version"] + 1,
                updated_at=now,
            )
        )
        log_history(session, ctx, ticket_id, "reopened", reason, now)
        return {"ticket_id": ticket_id, "status": "reopened", "version": row["version"] + 1}
