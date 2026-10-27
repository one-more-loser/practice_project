"""案例/Skill治理与反馈快照；没有个人原文的外部导出表。"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from cs_agent.db.business_tables import business_table, col

documents = business_table(
    "experience_document",
    col("document_id", primary_key=True),
    col("unit_id"),
    col("kind", String(12)),
    col("revision", Integer),
    col("name", String(160)),
    col("topic", String(40)),
    col("definition", JSONB),
    col("source_ticket_id", nullable=True),
    col("source_case_ids", JSONB),
    col("source_fingerprint", String(64)),
    col("policy_revision", String(80)),
    col("status", String(16)),
    col("created_by"),
    col("reviewed_by", nullable=True),
    col("reviewed_at", DateTime(timezone=True), nullable=True),
    col("published_at", DateTime(timezone=True), nullable=True),
    col("valid_from", DateTime(timezone=True)),
    col("valid_until", DateTime(timezone=True), nullable=True),
    constraints=(
        UniqueConstraint("kind", "unit_id", "revision"),
        ForeignKeyConstraint(["source_ticket_id"], ["support_ticket.ticket_id"]),
        CheckConstraint(
            "kind IN ('case','skill') AND status IN ('candidate','approved','published','retired')"
        ),
        CheckConstraint("revision>0 AND (valid_until IS NULL OR valid_until>valid_from)"),
    ),
)

regressions = business_table(
    "experience_regression",
    col("regression_id", primary_key=True),
    col("document_id"),
    col("definition_digest", String(64)),
    col("policy_revision", String(80)),
    col("suite_revision", String(80)),
    col("results", JSONB),
    col("passed", Boolean),
    col("executed_by"),
    constraints=(ForeignKeyConstraint(["document_id"], ["experience_document.document_id"]),),
)

feedback = business_table(
    "conversation_feedback",
    col("feedback_id", primary_key=True),
    col("conversation_id"),
    col("message_id"),
    col("owner_id"),
    col("rating", String(16)),
    col("topics", JSONB),
    col("comment", String(1000)),
    col("content_digest", String(64)),
    constraints=(
        ForeignKeyConstraint(["conversation_id"], ["conversation.conversation_id"]),
        ForeignKeyConstraint(["message_id"], ["conversation_message.message_id"]),
        UniqueConstraint("conversation_id", "message_id", "owner_id"),
        CheckConstraint("rating IN ('helpful','unhelpful')"),
    ),
)

snapshots = business_table(
    "support_summary_snapshot",
    col("snapshot_id", primary_key=True),
    col("window_start", DateTime(timezone=True)),
    col("window_end", DateTime(timezone=True)),
    col("revision", Integer),
    col("source_digest", String(64)),
    col("source_ids", JSONB),
    col("payload", JSONB),
    constraints=(
        UniqueConstraint(
            "store_id", "data_batch_id", "batch_generation", "window_start", "revision"
        ),
        CheckConstraint("revision>0 AND window_end>window_start"),
    ),
)
