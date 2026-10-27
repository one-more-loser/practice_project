"""T014独立action审核与原付款退款关联；共用业务来源字段。"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from cs_agent.db.business_tables import business_table, col

reviews = business_table(
    "cancellation_review",
    col("review_id", primary_key=True),
    col("task_kind", String(16), default="action"),
    col("operation_id"),
    col("confirmation_id"),
    col("order_id"),
    col("owner_id"),
    col("session_ref"),
    col("auth_version", Integer),
    col("request_id"),
    col("expected_order_version", Integer),
    col("status", String(32)),
    col("fence", Integer, default=0),
    col("attempts", Integer, default=0),
    col("executor_id", nullable=True),
    col("lease_until", DateTime(timezone=True), nullable=True),
    col("facts", JSONB, default=dict),
    col("facts_digest", String(64), nullable=True),
    col("checks", JSONB, default=list),
    col("rule_refs", JSONB, default=list),
    col("reason_codes", JSONB, default=list),
    col("basis_summary", JSONB, default=list),
    col("decision", String(24), nullable=True),
    col("model_identifier", String(160), nullable=True),
    col("model_revision", String(160), nullable=True),
    col("rules_revision", String(80)),
    col("skill_revision", String(80)),
    col("error_code", String(80), nullable=True),
    col("refund_id", nullable=True),
    col("entry_id", nullable=True),
    col("confirmed_at", DateTime(timezone=True)),
    col("started_at", DateTime(timezone=True), nullable=True),
    col("reviewed_at", DateTime(timezone=True), nullable=True),
    col("executed_at", DateTime(timezone=True), nullable=True),
    constraints=(
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        ForeignKeyConstraint(["confirmation_id"], ["action_confirmation.confirmation_id"]),
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
        ForeignKeyConstraint(["session_ref"], ["login_session.session_id"]),
        ForeignKeyConstraint(["entry_id"], ["wallet_entry.entry_id"]),
        UniqueConstraint("operation_id"),
        UniqueConstraint("confirmation_id"),
        CheckConstraint("task_kind='action' AND auth_version>0 AND expected_order_version>0"),
        CheckConstraint("fence>=0 AND attempts>=0"),
        CheckConstraint(
            "status IN ('queued','running','rejected','needs_facts','unavailable','execution_rejected','succeeded')"
        ),
        CheckConstraint("decision IS NULL OR decision IN ('approved','rejected','needs_facts')"),
        CheckConstraint(
            "status<>'succeeded' OR (decision='approved' AND refund_id IS NOT NULL AND entry_id IS NOT NULL AND executed_at IS NOT NULL)"
        ),
    ),
)
Index(
    "ix_cancellation_due",
    reviews.c.created_at,
    reviews.c.lease_until,
    postgresql_where=text("status IN ('queued','running')"),
)
Index("ix_cancellation_owner", reviews.c.owner_id, reviews.c.store_id, reviews.c.created_at)

refunds = business_table(
    "cancellation_refund",
    col("refund_id", primary_key=True),
    col("review_id"),
    col("operation_id"),
    col("order_id"),
    col("owner_id"),
    col("payment_id"),
    col("payment_entry_id"),
    col("entry_id"),
    col("amount_minor", BigInteger),
    col("goods_minor", BigInteger),
    col("freight_minor", BigInteger),
    col("currency", String(3), default="CNY"),
    col("refunded_at", DateTime(timezone=True)),
    constraints=(
        ForeignKeyConstraint(["review_id"], ["cancellation_review.review_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
        ForeignKeyConstraint(["payment_id"], ["order_payment.payment_id"]),
        ForeignKeyConstraint(["payment_entry_id"], ["wallet_entry.entry_id"]),
        ForeignKeyConstraint(["entry_id"], ["wallet_entry.entry_id"]),
        UniqueConstraint("review_id"),
        UniqueConstraint("operation_id"),
        UniqueConstraint("order_id"),
        UniqueConstraint("payment_id"),
        UniqueConstraint("entry_id"),
        CheckConstraint(
            "currency='CNY' AND amount_minor>0 AND goods_minor>=0 AND freight_minor>=0 AND amount_minor=goods_minor+freight_minor"
        ),
    ),
)
