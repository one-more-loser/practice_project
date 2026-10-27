"""活动表；使用现有 provenance，额度与终态均由本地短事务保护。"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from cs_agent.db import models as _models  # noqa: F401
from cs_agent.db.business_tables import business_table, col

SCOPE = ("store_id", "data_batch_id", "batch_generation")
previews = business_table(
    "activity_preview",
    col("preview_ref", primary_key=True),
    col("actor_ref"),
    col("auth_version", Integer),
    col("capability_revision", Integer),
    col("plan_id"),
    col("plan_version", Integer),
    col("manifest_digest", String(64)),
    col("manifest", JSONB),
    col("expires_at", DateTime(timezone=True)),
)

slots = business_table(
    "activity_execution_slot",
    col("intent_id", primary_key=True),
    col("plan_id"),
    col("plan_version", Integer),
    col("approval_ref"),
    col("manifest_digest", String(64)),
    col("manifest", JSONB),
    col("actor_ref"),
    col("actor_role", String(32)),
    col("session_ref"),
    col("auth_version", Integer),
    col("capability_revision", Integer),
    col("preview_ref"),
    col("grant_ref", nullable=True),
    col("grant_expires_at", DateTime(timezone=True), nullable=True),
    col("status", String(24), default="pending"),
    col("activity_id", nullable=True),
    col("receipt", JSONB, nullable=True),
    constraints=(
        UniqueConstraint(*SCOPE, "plan_id", "plan_version", "approval_ref"),
        UniqueConstraint(*SCOPE, "approval_ref"),
        CheckConstraint("status IN ('pending','committed','closed_no_effect')"),
    ),
)

activities = business_table(
    "activity_execution",
    col("activity_id", primary_key=True),
    col("intent_id"),
    col("manifest", JSONB),
    col("phase", String(16)),
    col("revision", Integer, default=1),
    col("start_at", DateTime(timezone=True)),
    col("end_at", DateTime(timezone=True)),
    col("variable_cap_minor", BigInteger),
    col("reserved_discount_minor", BigInteger, default=0),
    col("consumed_discount_minor", BigInteger, default=0),
    col("actual_revision", Integer, default=0),
    col("snapshot_id"),
    col("watermark", DateTime(timezone=True)),
    constraints=(
        UniqueConstraint("intent_id"),
        ForeignKeyConstraint(["intent_id"], ["activity_execution_slot.intent_id"]),
        CheckConstraint(
            "phase IN ('scheduled','active','paused','ended') AND revision>0 AND start_at<end_at"
        ),
        CheckConstraint(
            "variable_cap_minor>=0 AND reserved_discount_minor>=0 AND consumed_discount_minor>=0 AND reserved_discount_minor+consumed_discount_minor<=variable_cap_minor"
        ),
    ),
)

lines = business_table(
    "activity_execution_line",
    col("line_id", primary_key=True),
    col("activity_id"),
    col("sku_id"),
    col("base_price_minor", BigInteger),
    col("effective_price_minor", BigInteger),
    col("price_version", Integer),
    col("quantity_cap", BigInteger),
    col("reserved_qty", BigInteger, default=0),
    col("consumed_qty", BigInteger, default=0),
    constraints=(
        UniqueConstraint("activity_id", "sku_id"),
        ForeignKeyConstraint(["activity_id"], ["activity_execution.activity_id"]),
        ForeignKeyConstraint(["sku_id"], ["sku.sku_id"]),
        CheckConstraint(
            "quantity_cap>0 AND reserved_qty>=0 AND consumed_qty>=0 AND reserved_qty+consumed_qty<=quantity_cap"
        ),
        CheckConstraint(
            "effective_price_minor>0 AND base_price_minor>=effective_price_minor AND price_version>0"
        ),
    ),
)

allocations = business_table(
    "activity_order_allocation",
    col("allocation_id", primary_key=True),
    col("activity_id"),
    col("order_id"),
    col("sku_id"),
    col("qty", BigInteger),
    col("discount_minor", BigInteger),
    col("payment_minor", BigInteger),
    col("refund_minor", BigInteger, default=0),
    col("status", String(16), default="reserved"),
    constraints=(
        UniqueConstraint(*SCOPE, "order_id", "sku_id"),
        ForeignKeyConstraint(["activity_id"], ["activity_execution.activity_id"]),
        CheckConstraint("status IN ('reserved','consumed','released')"),
        CheckConstraint(
            "qty>0 AND discount_minor>=0 AND payment_minor>=0 AND refund_minor>=0 AND refund_minor<=payment_minor"
        ),
    ),
)

refunds = business_table(
    "activity_refund_event",
    col("event_id", primary_key=True),
    col("refund_id"),
    col("allocation_id"),
    col("amount_minor", BigInteger),
    constraints=(
        UniqueConstraint("refund_id", "allocation_id"),
        ForeignKeyConstraint(["allocation_id"], ["activity_order_allocation.allocation_id"]),
        CheckConstraint("amount_minor>=0"),
    ),
)

deliveries = business_table(
    "activity_delivery",
    col("event_id", primary_key=True),
    Column("sequence", BigInteger, Identity(), nullable=False),
    col("activity_id", nullable=True),
    col("intent_id"),
    col("kind", String(16)),
    col("payload", JSONB),
    col("status", String(16), default="pending"),
    col("attempts", Integer, default=0),
    col("available_at", DateTime(timezone=True)),
    col("lease_until", DateTime(timezone=True), nullable=True),
    col("lease_token", nullable=True),
    col("acked_at", DateTime(timezone=True), nullable=True),
    col("last_code", String(60), nullable=True),
    constraints=(
        CheckConstraint(
            "kind IN ('receipt','actuals') AND status IN ('pending','leased','acked') AND attempts>=0"
        ),
    ),
)

TABLES = (previews, slots, activities, lines, allocations, refunds, deliveries)

Index(
    "ix_activity_sku",
    lines.c.store_id,
    lines.c.data_batch_id,
    lines.c.batch_generation,
    lines.c.sku_id,
)
Index(
    "ix_activity_window",
    activities.c.store_id,
    activities.c.data_batch_id,
    activities.c.batch_generation,
    activities.c.phase,
    activities.c.end_at,
)
Index(
    "ix_activity_order",
    allocations.c.store_id,
    allocations.c.data_batch_id,
    allocations.c.batch_generation,
    allocations.c.order_id,
)
Index(
    "ix_activity_delivery_ready",
    deliveries.c.status,
    deliveries.c.available_at,
    deliveries.c.sequence,
)
Index("ix_activity_delivery_intent", deliveries.c.intent_id, deliveries.c.sequence)
