import json
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import make_url

metadata = MetaData()
snapshots = Table("ops_snapshots", metadata,
    Column("store_id", String(80), primary_key=True),
    Column("snapshot_id", String(80), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("digest", String(64), nullable=False), Column("body", JSON, nullable=False),
    Column("created_at", String(50), nullable=False))
plans = Table("ops_plans", metadata,
    Column("plan_id", String(80), primary_key=True), Column("store_id", String(80), nullable=False),
    Column("version", Integer, nullable=False), Column("status", String(40), nullable=False),
    Column("created_at", String(50), nullable=False), Column("updated_at", String(50), nullable=False))
revisions = Table("ops_plan_revisions", metadata,
    Column("plan_id", String(80), primary_key=True), Column("version", Integer, primary_key=True),
    Column("store_id", String(80), nullable=False), Column("body", JSON, nullable=False),
    Column("digest", String(64), nullable=False), Column("created_at", String(50), nullable=False))
evaluations = Table("ops_evaluations", metadata,
    Column("evaluation_id", String(80), primary_key=True),
    Column("plan_id", String(80), nullable=False), Column("version", Integer, nullable=False),
    Column("store_id", String(80), nullable=False), Column("body", JSON, nullable=False),
    Column("created_at", String(50), nullable=False))
approvals = Table("ops_approvals", metadata,
    Column("approval_ref", String(80), primary_key=True),
    Column("plan_id", String(80), nullable=False), Column("version", Integer, nullable=False),
    Column("store_id", String(80), nullable=False), Column("body", JSON, nullable=False),
    Column("created_at", String(50), nullable=False))
grants = Table("ops_execution_grants", metadata,
    Column("approval_ref", String(80), primary_key=True),
    Column("grant_ref", String(80), nullable=False, unique=True),
    Column("intent_id", String(80), nullable=False, unique=True),
    Column("store_id", String(80), nullable=False), Column("plan_id", String(80), nullable=False),
    Column("version", Integer, nullable=False), Column("digest", String(64), nullable=False),
    Column("body", JSON, nullable=False), Column("state", String(40), nullable=False),
    Column("created_at", String(50), nullable=False))
cancellations = Table("ops_approval_cancellations", metadata,
    Column("cancel_ref", String(80), primary_key=True),
    Column("plan_id", String(80), nullable=False), Column("version", Integer, nullable=False),
    Column("store_id", String(80), nullable=False), Column("body", JSON, nullable=False),
    Column("created_at", String(50), nullable=False))
receipts = Table("ops_execution_receipts", metadata,
    Column("event_id", String(100), primary_key=True),
    Column("store_id", String(80), nullable=False), Column("intent_id", String(80), nullable=False),
    Column("digest", String(64), nullable=False), Column("body", JSON, nullable=False),
    Column("created_at", String(50), nullable=False))
activities = Table("ops_activity_projection", metadata,
    Column("activity_id", String(80), primary_key=True),
    Column("store_id", String(80), nullable=False), Column("plan_id", String(80), nullable=False),
    Column("revision", Integer, nullable=False), Column("body", JSON, nullable=False))
actuals = Table("ops_activity_actuals", metadata,
    Column("store_id", String(80), primary_key=True),
    Column("snapshot_id", String(80), primary_key=True), Column("revision", Integer, primary_key=True),
    Column("activity_id", String(80), nullable=False), Column("digest", String(64), nullable=False),
    Column("body", JSON, nullable=False), Column("created_at", String(50), nullable=False))
operations = Table("ops_operations", metadata,
    Column("scope", String(250), primary_key=True), Column("digest", String(64), nullable=False),
    Column("result", JSON, nullable=False))
sessions = Table("ops_demo_sessions", metadata,
    Column("token_digest", String(64), primary_key=True), Column("body", JSON, nullable=False))
retrospectives = Table("ops_retrospectives", metadata,
    Column("retrospective_id", String(80), primary_key=True),
    Column("store_id", String(80), nullable=False), Column("plan_id", String(80), nullable=False),
    Column("body", JSON, nullable=False), Column("created_at", String(50), nullable=False))
audit = Table("ops_audit", metadata,
    Column("audit_id", String(80), primary_key=True), Column("store_id", String(80), nullable=False),
    Column("actor_ref", String(80), nullable=False), Column("action", String(80), nullable=False),
    Column("resource_ref", String(80)), Column("request_id", String(80)),
    Column("created_at", String(50), nullable=False))


SCHEMA_VERSION = 2
schema_versions = Table("ops_schema_versions", metadata,
    Column("version", Integer, primary_key=True), Column("description", String(200), nullable=False))


class Database:
    def __init__(self, url: str, mode: str):
        parsed = make_url(url)
        if mode == "integration":
            if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("ops02_"):
                raise ValueError("真实接入须使用名称以ops02_开头的02专用PostgreSQL库")
        self.mode = mode
        self.identity_record = None
        kwargs = {"pool_pre_ping": True}
        if parsed.get_backend_name() == "sqlite":
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 5}
        else:
            kwargs.update(pool_size=5, max_overflow=0, pool_timeout=3)
            kwargs["connect_args"] = {"connect_timeout": 3, "options": "-c statement_timeout=5000"}
        self.engine = create_engine(url, **kwargs)
        if mode == "integration":
            identity_file = os.environ.get("OPS_DATABASE_IDENTITY_FILE")
            if not identity_file:
                raise ValueError("真实接入须配置OPS_DATABASE_IDENTITY_FILE")
            expected = json.loads(Path(identity_file).read_text(encoding="utf-8-sig"))
            dsn = {"host": parsed.host, "port": parsed.port or 5432,
                   "database": parsed.database, "current_user": parsed.username}
            if any(expected.get(key) != value for key, value in dsn.items()):
                raise ValueError("02数据库DSN与独立身份清单不匹配")
            with self.engine.connect() as connection:
                actual = connection.execute(text("SELECT current_database() AS database, current_user AS current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")).mappings().one()
                guard = connection.execute(text("SELECT instance_id FROM ops_project_guard WHERE project_id='ops02'")).scalar_one()
                if (actual["database"] != expected["database"] or actual["current_user"] != expected["current_user"]
                        or actual["rolsuper"] or actual["rolbypassrls"] or guard != expected.get("instance_id")):
                    raise ValueError("02数据库实例、角色或权限隔离不匹配")
            self.identity_record = {**dsn, "instance_id": guard, "pool_size": 5, "max_overflow": 0}
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def sqlite_options(connection, _):
                connection.isolation_level = None
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")

            @event.listens_for(self.engine, "begin")
            def sqlite_begin(connection):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

    def initialize(self):
        # v1为已有metadata基线；v2只追加版本登记，不删除/重建任何已有表或数据。
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('ops02:migration',0))"))
            metadata.create_all(connection)
            versions = set(connection.execute(select(schema_versions.c.version)).scalars())
            if versions and max(versions) > SCHEMA_VERSION:
                raise ValueError("数据库版本高于当前代码，停止升级")
            for version, description in ((1, "existing metadata baseline"), (2, "replayable version ledger")):
                if version not in versions:
                    connection.execute(schema_versions.insert().values(version=version, description=description))
        self.verify_schema()

    def verify_schema(self):
        if not inspect(self.engine).has_table("ops_schema_versions"):
            raise ValueError("02数据库未升级；先备份再执行init-db")
        inspector = inspect(self.engine)
        for table in metadata.sorted_tables:
            if not inspector.has_table(table.name):
                raise ValueError("02数据库缺少表，拒绝ready")
            present = {column["name"] for column in inspector.get_columns(table.name)}
            if not set(table.c.keys()) <= present:
                raise ValueError("02数据库字段缺失，需要显式新增升级步骤")
        with self.engine.connect() as connection:
            versions = set(connection.execute(select(schema_versions.c.version)).scalars())
        if versions != set(range(1, SCHEMA_VERSION + 1)):
            raise ValueError("02数据库版本与运行代码不匹配")

    @contextmanager
    def transaction(self, lock_scope: str | None = None):
        from sqlalchemy import text
        with self.engine.begin() as connection:
            if lock_scope and self.engine.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                                   {"scope": lock_scope})
            yield connection

    def close(self):
        self.engine.dispose()
