"""显式运营授权与追加审计；冻结DDL，不导入运行时ORM。"""
from alembic import op

revision = "0021_operations_capabilities"
down_revision = "0020_agent_action_binding"


def upgrade():
    common = """
      source_kind varchar(32) NOT NULL CHECK(source_kind IN ('synthetic','derived','business_import')),
      data_batch_id varchar(80) NOT NULL, batch_generation integer NOT NULL,
      version integer NOT NULL DEFAULT 1 CHECK(version>0),
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(data_batch_id,batch_generation) REFERENCES source_batch(batch_id,generation)
    """
    op.execute(f"""
    CREATE TABLE operations_capability_state (
      subject_id varchar(80) PRIMARY KEY REFERENCES subject,
      store_id varchar(80) NOT NULL REFERENCES store,
      capability_revision integer NOT NULL DEFAULT 1 CHECK(capability_revision>0),
      {common}, FOREIGN KEY(subject_id,store_id) REFERENCES subject(subject_id,store_id)
    );
    CREATE TABLE operations_capability_grant (
      grant_id varchar(80) PRIMARY KEY,
      subject_id varchar(80) NOT NULL REFERENCES subject,
      store_id varchar(80) NOT NULL REFERENCES store,
      role varchar(32) NOT NULL CHECK(role IN ('support','supervisor','shop_admin','knowledge_admin')),
      capability varchar(40) NOT NULL CHECK(capability IN
        ('metrics.read','cost.read','data.import','plan.edit','plan.confirm','activity.publish','activity.manage')),
      grant_status varchar(16) NOT NULL CHECK(grant_status IN ('active','revoked','expired')),
      valid_from timestamptz NOT NULL, valid_to timestamptz, revoked_at timestamptz,
      {common}, FOREIGN KEY(subject_id,store_id) REFERENCES subject(subject_id,store_id),
      CHECK(valid_to IS NULL OR valid_to>valid_from),
      CHECK(grant_status='active' OR revoked_at IS NOT NULL)
    );
    CREATE INDEX ix_operations_grant_subject ON operations_capability_grant(subject_id,store_id,grant_status);
    CREATE TABLE operations_capability_audit (
      event_id varchar(80) PRIMARY KEY,
      subject_id varchar(80) NOT NULL REFERENCES subject,
      store_id varchar(80) NOT NULL REFERENCES store,
      grant_id varchar(80) NOT NULL REFERENCES operations_capability_grant,
      actor_ref varchar(80) NOT NULL, action varchar(16) NOT NULL
        CHECK(action IN ('granted','revoked','expired')),
      reason varchar(1000) NOT NULL CHECK(length(reason)>0),
      capability_revision integer NOT NULL CHECK(capability_revision>0),
      {common}, FOREIGN KEY(subject_id,store_id) REFERENCES subject(subject_id,store_id),
      UNIQUE(subject_id,capability_revision)
    );
    GRANT SELECT,INSERT,UPDATE ON operations_capability_state,operations_capability_grant TO cs_agent_app_b0;
    GRANT SELECT,INSERT ON operations_capability_audit TO cs_agent_app_b0;
    """)
    # 与受控查询层相同的服务角色RLS；查询角色无GRANT/无策略，不能读授权记录。
    for table in ("operations_capability_state", "operations_capability_grant",
                  "operations_capability_audit"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY operations_identity_service ON {table} "
                   "TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true)")


def downgrade():
    raise RuntimeError("授权审计不自动破坏性降级")
