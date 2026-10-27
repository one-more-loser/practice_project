from dataclasses import dataclass
from uuid import uuid4

from cs_agent.core.context import Role
from cs_agent.db.models import AuditEvent
from cs_agent.privacy.redaction import safe_summary


@dataclass(frozen=True)
class AuditIdentity:
    # 仅作已识别会话的拒绝归因，不具有TrustedContext的授权语义。
    subject_id: str
    store_id: str
    role: Role
    auth_version: int
    data_batch_id: str
    batch_generation: int


def record(session, *, request_id, action, outcome, ctx=None, resource_ref=None, summary=None):
    summary = dict(summary or {})
    if ctx:
        summary.update(auth_version=ctx.auth_version, batch_generation=ctx.batch_generation)
    session.add(
        AuditEvent(
            audit_id="aud_" + uuid4().hex,
            request_id=request_id,
            actor_id=ctx.subject_id if ctx else None,
            store_id=ctx.store_id if ctx else None,
            role=ctx.role.value if ctx else None,
            action=action,
            outcome=outcome,
            resource_ref=resource_ref,
            summary=safe_summary(summary),
            data_batch_id=ctx.data_batch_id if ctx else None,
            batch_generation=ctx.batch_generation if ctx else None,
        )
    )
