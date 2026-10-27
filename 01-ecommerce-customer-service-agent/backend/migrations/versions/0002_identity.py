"""可信身份、受控收货输入读取及只追加审计；不包含交易或工单。"""

from alembic import op

revision = "0002_identity"
down_revision = "0001_source"


def common():
    # 迁移内冻结字段，不能导入未来ORM来改变历史迁移。
    return """
      source_kind varchar(32) NOT NULL CHECK(source_kind IN ('synthetic','business_import','derived')),
      data_batch_id varchar(80) NOT NULL, batch_generation integer NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      version integer NOT NULL DEFAULT 1 CHECK(version>0),
      FOREIGN KEY(data_batch_id,batch_generation) REFERENCES source_batch(batch_id,generation)
    """


def upgrade():
    op.execute(f"""
    CREATE TABLE subject (
      subject_id varchar(80) PRIMARY KEY, store_id varchar(80) NOT NULL REFERENCES store,
      customer_id varchar(80) UNIQUE,
      subject_type varchar(16) NOT NULL CHECK(subject_type IN ('customer','staff')),
      login_name varchar(80) NOT NULL UNIQUE, password_digest text NOT NULL,
      account_status varchar(16) NOT NULL CHECK(account_status IN ('active','disabled')),
      auth_version integer NOT NULL CHECK(auth_version>0),
      {common()}, UNIQUE(subject_id,store_id),
      CHECK((subject_type='customer' AND customer_id IS NOT NULL) OR (subject_type='staff' AND customer_id IS NULL))
    );
    CREATE TABLE staff_role_grant (
      grant_id varchar(80) PRIMARY KEY, staff_id varchar(80) NOT NULL,
      store_id varchar(80) NOT NULL REFERENCES store,
      role varchar(32) NOT NULL CHECK(role IN ('support','supervisor','shop_admin','knowledge_admin')),
      grant_status varchar(16) NOT NULL CHECK(grant_status IN ('active','revoked')),
      valid_from timestamptz NOT NULL, valid_to timestamptz, revoked_at timestamptz,
      revocation_reason varchar(80), {common()},
      FOREIGN KEY(staff_id,store_id) REFERENCES subject(subject_id,store_id),
      CHECK(valid_to IS NULL OR valid_to>valid_from),
      CHECK(grant_status<>'revoked' OR (revoked_at IS NOT NULL AND revocation_reason IS NOT NULL))
    );
    CREATE TABLE login_session (
      session_id varchar(80) PRIMARY KEY, subject_id varchar(80) NOT NULL REFERENCES subject,
      token_digest varchar(64) NOT NULL UNIQUE, csrf_digest varchar(64) NOT NULL,
      active_role varchar(32) NOT NULL CHECK(active_role IN ('customer','support','supervisor','shop_admin','knowledge_admin')),
      auth_version integer NOT NULL CHECK(auth_version>0),
      status varchar(16) NOT NULL CHECK(status IN ('active','revoked')),
      expires_at timestamptz NOT NULL, revoked_at timestamptz, {common()},
      CHECK(status<>'revoked' OR revoked_at IS NOT NULL)
    );
    CREATE TABLE shipping_input (
      input_id varchar(80) PRIMARY KEY, owner_id varchar(80) NOT NULL,
      store_id varchar(80) NOT NULL REFERENCES store,
      recipient varchar(100) NOT NULL, phone varchar(32) NOT NULL CHECK(phone ~ '^1[3-9][0-9]{{9}}$'),
      province varchar(80) NOT NULL, city varchar(80) NOT NULL,
      district varchar(80) NOT NULL, street varchar(300) NOT NULL, postal_code varchar(16),
      purpose varchar(32) NOT NULL CHECK(purpose IN ('checkout','address_change')),
      {common()}, FOREIGN KEY(owner_id,store_id) REFERENCES subject(subject_id,store_id)
    );
    CREATE TABLE audit_event (
      audit_id varchar(80) PRIMARY KEY, request_id varchar(80) NOT NULL,
      actor_id varchar(80), store_id varchar(80), role varchar(32),
      action varchar(40) NOT NULL, outcome varchar(16) NOT NULL CHECK(outcome IN ('allowed','denied','succeeded','failed')),
      resource_ref varchar(80), summary jsonb NOT NULL,
      occurred_at timestamptz NOT NULL DEFAULT now(), recorded_at timestamptz NOT NULL DEFAULT now(),
      source_kind varchar(32) NOT NULL CHECK(source_kind='derived'),
      data_batch_id varchar(80), batch_generation integer,
      CHECK((data_batch_id IS NULL)=(batch_generation IS NULL))
    );
    CREATE INDEX ix_role_grant_subject ON staff_role_grant(staff_id,store_id,grant_status);
    CREATE INDEX ix_session_expiry ON login_session(expires_at) WHERE status='active';
    CREATE INDEX ix_shipping_owner ON shipping_input(owner_id,store_id);
    CREATE INDEX ix_audit_request ON audit_event(request_id);
    GRANT SELECT ON subject,staff_role_grant,shipping_input TO cs_agent_app_b0;
    GRANT SELECT,INSERT,UPDATE ON login_session TO cs_agent_app_b0;
    GRANT INSERT ON audit_event TO cs_agent_app_b0;
    """)


def downgrade():
    raise RuntimeError("禁止自动破坏性降级；先评审备份/数据兼容与隔离恢复方案")
