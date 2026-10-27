"""经营快照固定身份与持久单调观察修订。"""
from alembic import op

revision = "0023_operations_snapshot"
down_revision = "0022_activity_execution"


def upgrade():
    op.execute("""
    CREATE TABLE operations_snapshot_state (
      snapshot_id varchar(80) PRIMARY KEY,
      revision integer NOT NULL CHECK(revision>0), source_digest varchar(64) NOT NULL,
      observed_at timestamptz NOT NULL, payload jsonb NOT NULL,
      store_id varchar(80) NOT NULL REFERENCES store,
      source_kind varchar(32) NOT NULL CHECK(source_kind IN ('synthetic','derived','business_import')),
      data_batch_id varchar(80) NOT NULL, batch_generation integer NOT NULL,
      version integer NOT NULL DEFAULT 1 CHECK(version>0),
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(data_batch_id,batch_generation) REFERENCES source_batch(batch_id,generation),
      UNIQUE(store_id,data_batch_id,batch_generation)
    );
    GRANT SELECT,INSERT,UPDATE ON operations_snapshot_state TO cs_agent_app_b0;
    ALTER TABLE operations_snapshot_state ENABLE ROW LEVEL SECURITY;
    ALTER TABLE operations_snapshot_state FORCE ROW LEVEL SECURITY;
    CREATE POLICY operations_snapshot_service ON operations_snapshot_state
      TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true);
    """)


def downgrade():
    raise RuntimeError("经营快照修订不自动破坏性降级")
