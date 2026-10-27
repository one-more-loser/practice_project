from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Provenance:
    source_kind: Mapped[str] = mapped_column(String(32), default="synthetic")
    data_batch_id: Mapped[str] = mapped_column(String(80))
    batch_generation: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    version: Mapped[int] = mapped_column(Integer, default=1)


class SourceBatch(Base):
    __tablename__ = "source_batch"
    batch_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    source_kind: Mapped[str] = mapped_column(String(32), default="synthetic")
    seed: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    manifest_revision: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Store(Provenance, Base):
    __tablename__ = "store"
    store_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="active")
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    business_timezone: Mapped[str] = mapped_column(String(40), default="Asia/Shanghai")


class Subject(Provenance, Base):
    __tablename__ = "subject"
    subject_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.store_id"))
    customer_id: Mapped[str | None] = mapped_column(String(80), unique=True)
    subject_type: Mapped[str] = mapped_column(String(16))
    login_name: Mapped[str] = mapped_column(String(80), unique=True)
    password_digest: Mapped[str] = mapped_column(Text)
    account_status: Mapped[str] = mapped_column(String(16), default="active")
    auth_version: Mapped[int] = mapped_column(Integer, default=1)


class RoleGrant(Provenance, Base):
    __tablename__ = "staff_role_grant"
    grant_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    staff_id: Mapped[str] = mapped_column(String(80))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.store_id"))
    role: Mapped[str] = mapped_column(String(32))
    grant_status: Mapped[str] = mapped_column(String(16), default="active")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(String(80))


class LoginSession(Provenance, Base):
    __tablename__ = "login_session"
    session_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("subject.subject_id"))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_digest: Mapped[str] = mapped_column(String(64))
    active_role: Mapped[str] = mapped_column(String(32))
    auth_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ShippingInput(Provenance, Base):
    # B0仅用于本人读取/投影边界。写入表单及订单快照关联由T-CS-006补齐。
    __tablename__ = "shipping_input"
    input_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.store_id"))
    recipient: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(32))
    province: Mapped[str] = mapped_column(String(80))
    city: Mapped[str] = mapped_column(String(80))
    district: Mapped[str] = mapped_column(String(80))
    street: Mapped[str] = mapped_column(String(300))
    postal_code: Mapped[str | None] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(32), default="checkout")


class AuditEvent(Base):
    __tablename__ = "audit_event"
    audit_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(80))
    actor_id: Mapped[str | None] = mapped_column(String(80))
    store_id: Mapped[str | None] = mapped_column(String(80))
    role: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(40))
    outcome: Mapped[str] = mapped_column(String(16))
    resource_ref: Mapped[str | None] = mapped_column(String(80))
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    source_kind: Mapped[str] = mapped_column(String(32), default="derived")
    data_batch_id: Mapped[str | None] = mapped_column(String(80))
    batch_generation: Mapped[int | None] = mapped_column(Integer)


# 与已冻结迁移保持一致，避免后续autogenerate误删批次和店铺复合约束。
SourceBatch.__table__.append_constraint(UniqueConstraint("batch_id", "generation"))
Subject.__table__.append_constraint(UniqueConstraint("subject_id", "store_id"))
for entity in (Store, Subject, RoleGrant, LoginSession, ShippingInput):
    entity.__table__.append_constraint(
        ForeignKeyConstraint(
            ["data_batch_id", "batch_generation"],
            ["source_batch.batch_id", "source_batch.generation"],
        )
    )
RoleGrant.__table__.append_constraint(
    ForeignKeyConstraint(["staff_id", "store_id"], ["subject.subject_id", "subject.store_id"])
)
ShippingInput.__table__.append_constraint(
    ForeignKeyConstraint(["owner_id", "store_id"], ["subject.subject_id", "subject.store_id"])
)
Index("ix_role_grant_subject", RoleGrant.staff_id, RoleGrant.store_id, RoleGrant.grant_status)
Index("ix_session_expiry", LoginSession.expires_at, postgresql_where=text("status='active'"))
Index("ix_shipping_owner", ShippingInput.owner_id, ShippingInput.store_id)
Index("ix_audit_request", AuditEvent.request_id)

# 核心业务使用同一metadata，供迁移比较与测试核对，不执行create_all。
from cs_agent.db import business_tables as _business_tables  # noqa: E402,F401
