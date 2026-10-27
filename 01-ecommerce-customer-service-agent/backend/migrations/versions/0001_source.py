"""来源与店铺骨架。"""

from alembic import op

revision = "0001_source"
down_revision = None


def upgrade():
    op.execute("""
    CREATE TABLE source_batch (
      batch_id varchar(80) PRIMARY KEY, generation integer NOT NULL CHECK(generation>0),
      source_kind varchar(32) NOT NULL CHECK(source_kind IN ('synthetic','business_import','derived')),
      seed integer NOT NULL, fingerprint varchar(64) NOT NULL CHECK(length(fingerprint)=64),
      manifest_revision varchar(40) NOT NULL, status varchar(16) NOT NULL CHECK(status IN ('active','retired')),
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(batch_id,generation)
    );
    CREATE TABLE store (
      store_id varchar(80) PRIMARY KEY, name varchar(100) NOT NULL,
      status varchar(16) NOT NULL CHECK(status IN ('active','disabled')),
      currency varchar(3) NOT NULL CHECK(currency='CNY'), business_timezone varchar(40) NOT NULL,
      source_kind varchar(32) NOT NULL CHECK(source_kind IN ('synthetic','business_import','derived')),
      data_batch_id varchar(80) NOT NULL, batch_generation integer NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      version integer NOT NULL DEFAULT 1 CHECK(version>0),
      FOREIGN KEY(data_batch_id,batch_generation) REFERENCES source_batch(batch_id,generation)
    );
    GRANT SELECT ON source_batch,store TO cs_agent_app_b0;
    """)


def downgrade():
    raise RuntimeError("B0禁止自动回退删除数据；仅在升级测试专用库中清理已核对表")
