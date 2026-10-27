from sqlalchemy import CheckConstraint, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from cs_agent.db.business_tables import business_table, col

snapshot_states = business_table(
    "operations_snapshot_state",
    col("snapshot_id", primary_key=True),
    col("revision", Integer),
    col("source_digest", String(64)),
    col("observed_at", DateTime(timezone=True)),
    col("payload", JSONB),
    constraints=(
        UniqueConstraint("store_id", "data_batch_id", "batch_generation"),
        CheckConstraint("revision>0"),
    ),
)
