from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from cs_agent.db.business_tables import business_table, col

guest_sessions = business_table(
    "guest_session",
    col("guest_id", primary_key=True),
    col("token_digest", String(64)),
    col("expires_at", DateTime(timezone=True)),
    col("status", String(16), default="active"),
    constraints=(UniqueConstraint("token_digest"),),
)
conversations = business_table(
    "conversation",
    col("conversation_id", primary_key=True),
    col("owner_id"),
    col("principal_kind", String(16)),
    col("status", String(16), default="open"),
    col("mode", String(16), default="agent"),
    col("mode_epoch", Integer, default=1),
    col("last_seq", Integer, default=0),
    col("floor_seq", Integer, default=0),
    col("next_message_seq", Integer, default=1),
    col("active_run_id", nullable=True),
    col("dialog_state", JSONB, default=dict),
)
messages = business_table(
    "conversation_message",
    col("message_id", primary_key=True),
    col("conversation_id"),
    col("source_role", String(16)),
    col("message_seq", Integer),
    col("text", String(6000)),
    col("citations", JSONB, default=list),
    col("client_message_id", String(128)),
    col("content_digest", String(64)),
    col("run_id", nullable=True),
    col("status", String(24), default="final"),
    constraints=(
        ForeignKeyConstraint(["conversation_id"], ["conversation.conversation_id"]),
        UniqueConstraint("conversation_id", "source_role", "client_message_id"),
        UniqueConstraint("conversation_id", "message_seq", name="uq_message_conversation_seq"),
    ),
)
runs = business_table(
    "agent_run",
    col("run_id", primary_key=True),
    col("conversation_id"),
    col("message_id"),
    col("owner_id"),
    col("session_ref"),
    col("auth_version", Integer),
    col("principal_kind", String(16)),
    col("context_kind", String(24), default="conversation"),
    col("mode_epoch", Integer),
    col("status", String(24), default="queued"),
    col("graph_version", String(40), default="CS-GRAPH-B2-v1"),
    col("checkpoint_thread"),
    col("error_code", String(48), nullable=True),
    constraints=(
        ForeignKeyConstraint(["conversation_id"], ["conversation.conversation_id"]),
        ForeignKeyConstraint(["message_id"], ["conversation_message.message_id"]),
        UniqueConstraint("message_id"),
    ),
)
tasks = business_table(
    "agent_task",
    col("task_id", primary_key=True),
    col("run_id"),
    col("status", String(24), default="queued"),
    col("fence", Integer, default=0),
    col("executor_id", String(80), nullable=True),
    col("lease_until", DateTime(timezone=True), nullable=True),
    col("attempts", Integer, default=0),
    constraints=(
        ForeignKeyConstraint(["run_id"], ["agent_run.run_id"]),
        UniqueConstraint("run_id"),
    ),
)
events = business_table(
    "conversation_event",
    col("event_id", primary_key=True),
    col("conversation_id"),
    col("stream_id"),
    col("seq", Integer),
    col("mode_epoch", Integer),
    col("type", String(48)),
    col("data", JSONB),
    constraints=(
        ForeignKeyConstraint(["conversation_id"], ["conversation.conversation_id"]),
        UniqueConstraint("stream_id", "seq"),
    ),
)
Index("ix_conversation_owner", conversations.c.owner_id, conversations.c.store_id)
Index("ix_runtime_claim", tasks.c.status, tasks.c.lease_until)
Index("ix_message_history", messages.c.conversation_id, messages.c.created_at)
Index("ix_message_history_seq", messages.c.conversation_id, messages.c.message_seq)
