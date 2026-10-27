"""本地可信授权；调用方拥有事务，函数不提交、不进行HTTP。"""
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select, text

from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.models import LoginSession, Subject
from cs_agent.db.operations_identity_tables import (
    OperationsCapabilityAudit as Audit,
)
from cs_agent.db.operations_identity_tables import (
    OperationsCapabilityGrant as Grant,
)
from cs_agent.db.operations_identity_tables import (
    OperationsCapabilityState as State,
)

CAPABILITIES = frozenset({"metrics.read", "cost.read", "data.import", "plan.edit",
                          "plan.confirm", "activity.publish", "activity.manage"})


@dataclass(frozen=True)
class OperationsProjection:
    capabilities: tuple[str, ...]
    capability_revision: int
    auth_version: int


def capability_projection(ctx, grants, revision, now):
    capabilities = {g.capability for g in grants if (
        ctx.role != Role.CUSTOMER and g.subject_id == ctx.subject_id
        and g.store_id == ctx.store_id and g.role == ctx.role.value
        and g.data_batch_id == ctx.data_batch_id and g.batch_generation == ctx.batch_generation
        and g.grant_status == "active" and g.valid_from <= now
        and (g.valid_to is None or g.valid_to > now) and g.capability in CAPABILITIES
    )}
    return OperationsProjection(tuple(sorted(capabilities)), revision, ctx.auth_version)


def enforce_projection(projection, capability, expected_auth_version=None,
                       expected_capability_revision=None):
    if capability not in CAPABILITIES or capability not in projection.capabilities:
        raise AppError("forbidden", 403, "当前身份不具备运营能力")
    if ((expected_auth_version is not None and expected_auth_version != projection.auth_version)
        or (expected_capability_revision is not None
            and expected_capability_revision != projection.capability_revision)):
        raise AppError("identity_version_conflict", 409, "身份授权已变化，请重新确认")


def lock_operations_subject(session, subject_id):
    # 独立命名空间；发布、授予、撤销、过期处理必须持有到事务结束。
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": "cs01:operations-subject:" + subject_id})


def _state(session, subject):
    state = session.get(State, subject.subject_id, populate_existing=True)
    if state is None:
        state = State(subject_id=subject.subject_id, store_id=subject.store_id,
                      capability_revision=1, source_kind="derived",
                      data_batch_id=subject.data_batch_id, batch_generation=subject.batch_generation)
        session.add(state)
        session.flush()
    if (state.store_id != subject.store_id or state.data_batch_id != subject.data_batch_id
            or state.batch_generation != subject.batch_generation):
        raise AppError("unauthenticated", 401, "请重新登录")
    return state


def _audit(session, subject, state, grant, action, actor_ref, reason):
    state.capability_revision += 1
    state.version += 1
    session.add(Audit(event_id="oca_" + uuid4().hex, subject_id=subject.subject_id,
                      store_id=subject.store_id, grant_id=grant.grant_id, actor_ref=actor_ref,
                      action=action, reason=reason, capability_revision=state.capability_revision,
                      source_kind="derived", data_batch_id=subject.data_batch_id,
                      batch_generation=subject.batch_generation))


def operations_for(session, ctx, now):
    lock_operations_subject(session, ctx.subject_id)
    subject = session.get(Subject, ctx.subject_id, populate_existing=True)
    if subject is None or subject.store_id != ctx.store_id:
        raise not_found()
    state = _state(session, subject)
    grants = list(session.scalars(select(Grant).where(
        Grant.subject_id == ctx.subject_id, Grant.store_id == ctx.store_id,
        Grant.data_batch_id == ctx.data_batch_id, Grant.batch_generation == ctx.batch_generation
    ).execution_options(populate_existing=True)))
    for grant in grants:
        if grant.grant_status == "active" and grant.valid_to is not None and grant.valid_to <= now:
            grant.grant_status, grant.revoked_at = "expired", now
            _audit(session, subject, state, grant, "expired", "system:clock", "valid_to_elapsed")
    session.flush()
    return capability_projection(ctx, grants, state.capability_revision, now)


def require_operations(session, ctx, capability, now, *, lock=False,
                       expected_auth_version=None, expected_capability_revision=None):
    from cs_agent.identity.service import check_subject, roles_for

    # 先session行锁再subject advisory，与authenticate → session_view的顺序一致。
    query = select(LoginSession).where(LoginSession.session_id == ctx.session_ref)
    if lock:
        query = query.with_for_update()
    current = session.scalar(query.execution_options(populate_existing=True))
    lock_operations_subject(session, ctx.subject_id)
    subject = session.get(Subject, ctx.subject_id, populate_existing=True)
    check_subject(session, subject)
    if (current is None or current.subject_id != ctx.subject_id
        or current.status != "active" or current.expires_at <= now
        or subject.store_id != ctx.store_id or subject.auth_version != ctx.auth_version
        or current.auth_version != ctx.auth_version or current.active_role != ctx.role.value
        or subject.data_batch_id != ctx.data_batch_id
        or subject.batch_generation != ctx.batch_generation
        or current.data_batch_id != ctx.data_batch_id
        or current.batch_generation != ctx.batch_generation
        or ctx.role not in roles_for(session, subject, now)):
        raise AppError("unauthenticated", 401, "请重新登录")
    projection = operations_for(session, ctx, now)
    enforce_projection(projection, capability, expected_auth_version, expected_capability_revision)
    return projection


def grant_capability(session, *, subject_id, store_id, role, capability, now,
                     valid_to=None, actor_ref, reason):
    """受控管理/seed入口，不暴露HTTP；actor_ref是审计标识而非授权凭证。"""
    from cs_agent.identity.service import check_subject, roles_for

    if capability not in CAPABILITIES or role == Role.CUSTOMER:
        raise AppError("invalid_capability", 422, "运营能力或岗位无效")
    if not actor_ref or not reason.strip() or len(reason) > 1000:
        raise AppError("invalid_grant_reason", 422, "授权须提供原因与操作人")
    if valid_to is not None and valid_to <= now:
        raise AppError("invalid_grant_window", 422, "授权有效期无效")
    lock_operations_subject(session, subject_id)
    subject = session.get(Subject, subject_id, populate_existing=True)
    if subject is None or subject.store_id != store_id:
        raise not_found()
    check_subject(session, subject)
    if subject.subject_type != "staff" or role not in roles_for(session, subject, now):
        raise AppError("forbidden", 403, "不能向未持有的岗位授予运营能力")
    state = _state(session, subject)
    grant = Grant(grant_id="ocg_" + uuid4().hex, subject_id=subject_id,
                  store_id=store_id, role=role.value, capability=capability,
                  grant_status="active", valid_from=now, valid_to=valid_to,
                  source_kind="derived", data_batch_id=subject.data_batch_id,
                  batch_generation=subject.batch_generation)
    session.add(grant)
    session.flush()
    _audit(session, subject, state, grant, "granted", actor_ref, reason)
    session.flush()
    return grant


def revoke_capability(session, *, subject_id, store_id, grant_id, now, actor_ref, reason):
    if not actor_ref or not reason.strip() or len(reason) > 1000:
        raise AppError("invalid_grant_reason", 422, "撤权须提供原因与操作人")
    lock_operations_subject(session, subject_id)
    subject = session.get(Subject, subject_id, populate_existing=True)
    grant = session.get(Grant, grant_id, populate_existing=True)
    if (subject is None or subject.store_id != store_id or grant is None
        or grant.subject_id != subject_id or grant.store_id != store_id
        or grant.data_batch_id != subject.data_batch_id
        or grant.batch_generation != subject.batch_generation):
        raise not_found()
    if grant.grant_status == "active":
        grant.grant_status, grant.revoked_at = "revoked", now
        _audit(session, subject, _state(session, subject), grant, "revoked", actor_ref, reason)
        session.flush()
    return grant
