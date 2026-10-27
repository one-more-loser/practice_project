"""运营授权独立于客服岗位；审计仅追加。"""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from cs_agent.db.models import Base, Provenance


def common_constraints():
    return (
        ForeignKeyConstraint(["subject_id", "store_id"], ["subject.subject_id", "subject.store_id"]),
        ForeignKeyConstraint(["data_batch_id", "batch_generation"],
                             ["source_batch.batch_id", "source_batch.generation"]),
        CheckConstraint("version>0"),
        CheckConstraint("source_kind IN ('synthetic','derived','business_import')"),
    )


class OperationsCapabilityState(Provenance, Base):
    __tablename__ = "operations_capability_state"
    __table_args__ = (*common_constraints(), CheckConstraint("capability_revision>0"))
    subject_id: Mapped[str] = mapped_column(ForeignKey("subject.subject_id"), primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.store_id"))
    capability_revision: Mapped[int] = mapped_column(Integer, default=1)


class OperationsCapabilityGrant(Provenance, Base):
    __tablename__ = "operations_capability_grant"
    __table_args__ = (
        *common_constraints(),
        CheckConstraint("role IN ('support','supervisor','shop_admin','knowledge_admin')"),
        CheckConstraint("capability IN ('metrics.read','cost.read','data.import','plan.edit',"
                        "'plan.confirm','activity.publish','activity.manage')"),
        CheckConstraint("grant_status IN ('active','revoked','expired')"),
        CheckConstraint("valid_to IS NULL OR valid_to>valid_from"),
        CheckConstraint("grant_status='active' OR revoked_at IS NOT NULL"),
        Index("ix_operations_grant_subject", "subject_id", "store_id", "grant_status"),
    )
    grant_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("subject.subject_id"))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.store_id"))
    role: Mapped[str] = mapped_column(String(32))
    capability: Mapped[str] = mapped_column(String(40))
    grant_status: Mapped[str] = mapped_column(String(16), default="active")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationsCapabilityAudit(Provenance, Base):
    __tablename__ = "operations_capability_audit"
    __table_args__ = (
        *common_constraints(), CheckConstraint("action IN ('granted','revoked','expired')"),
        CheckConstraint("length(reason)>0"), CheckConstraint("capability_revision>0"),
        UniqueConstraint("subject_id", "capability_revision"),
    )
    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("subject.subject_id"))
    store_id: Mapped[str] = mapped_column(ForeignKey("store.store_id"))
    grant_id: Mapped[str] = mapped_column(ForeignKey("operations_capability_grant.grant_id"))
    actor_ref: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(1000))
    capability_revision: Mapped[int] = mapped_column(Integer)
