"""工单及人工协同持久事实；会话消息和事件复用runtime表。"""

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from cs_agent.db.business_tables import business_table, col

tickets = business_table(
    "support_ticket",
    col("ticket_id", primary_key=True),
    col("owner_id"),
    col("conversation_id", nullable=True),
    col("order_id", nullable=True),
    col("subject", String(160)),
    col("summary", String(500)),
    col("category", String(32)),
    col("priority", String(16), default="normal"),
    col("priority_reason", String(500)),
    col("status", String(24), default="open"),
    col("assigned_staff_id", nullable=True),
    col("assignment_id", nullable=True),
    col("first_reply_at", DateTime(timezone=True), nullable=True),
    col("sla_due_at", DateTime(timezone=True)),
    col("escalated_at", DateTime(timezone=True), nullable=True),
    col("resolved_at", DateTime(timezone=True), nullable=True),
    col("closed_at", DateTime(timezone=True), nullable=True),
    col("linked_from_ticket_id", nullable=True),
    col("case_kind", String(32), nullable=True),
    col("case_ref", nullable=True),
    constraints=(
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
        ForeignKeyConstraint(["conversation_id"], ["conversation.conversation_id"]),
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["linked_from_ticket_id"], ["support_ticket.ticket_id"]),
        CheckConstraint("priority IN ('normal','high')"),
        CheckConstraint(
            "status IN ('open','assigned','in_progress','waiting_customer','resolved','closed','reopened')"
        ),
    ),
)
assignments = business_table(
    "support_assignment",
    col("assignment_id", primary_key=True),
    col("ticket_id"),
    col("staff_id"),
    col("assigned_by"),
    col("reason", String(500)),
    col("status", String(16), default="active"),
    col("revoked_at", DateTime(timezone=True), nullable=True),
    constraints=(
        ForeignKeyConstraint(["ticket_id"], ["support_ticket.ticket_id"]),
        ForeignKeyConstraint(["staff_id"], ["subject.subject_id"]),
        CheckConstraint("status IN ('active','revoked')"),
    ),
)
entries = business_table(
    "support_entry",
    col("entry_id", primary_key=True),
    col("ticket_id"),
    col("actor_id"),
    col("source_role", String(16)),
    col("visibility", String(16)),
    col("text", String(4000)),
    col("client_message_id", String(128), nullable=True),
    constraints=(
        ForeignKeyConstraint(["ticket_id"], ["support_ticket.ticket_id"]),
        CheckConstraint("visibility IN ('public','internal')"),
        UniqueConstraint("ticket_id", "actor_id", "client_message_id"),
    ),
)
history = business_table(
    "support_history",
    col("history_id", primary_key=True),
    col("ticket_id"),
    col("action", String(48)),
    col("actor_id"),
    col("reason", String(500)),
    col("data", JSONB, default=dict),
    constraints=(ForeignKeyConstraint(["ticket_id"], ["support_ticket.ticket_id"]),),
)
human_sessions = business_table(
    "human_session",
    col("human_session_id", primary_key=True),
    col("conversation_id"),
    col("ticket_id"),
    col("assignment_id", nullable=True),
    col("staff_id", nullable=True),
    col("status", String(16)),
    col("requested_at", DateTime(timezone=True)),
    col("started_at", DateTime(timezone=True), nullable=True),
    col("ended_at", DateTime(timezone=True), nullable=True),
    col("end_reason", String(500), nullable=True),
    constraints=(
        ForeignKeyConstraint(["conversation_id"], ["conversation.conversation_id"]),
        ForeignKeyConstraint(["ticket_id"], ["support_ticket.ticket_id"]),
        ForeignKeyConstraint(["assignment_id"], ["support_assignment.assignment_id"]),
        CheckConstraint("status IN ('queued','human','ended','cancelled')"),
    ),
)
Index("ix_support_queue", tickets.c.store_id, tickets.c.status, tickets.c.sla_due_at)
Index("ix_support_owner", tickets.c.owner_id, tickets.c.created_at)
Index(
    "uq_support_active_assignment",
    assignments.c.ticket_id,
    unique=True,
    postgresql_where=text("status = 'active'"),
)
Index(
    "uq_human_active_conversation",
    human_sessions.c.conversation_id,
    unique=True,
    postgresql_where=text("status IN ('queued','human')"),
)
Index("ix_support_entry_history", entries.c.ticket_id, entries.c.created_at)
