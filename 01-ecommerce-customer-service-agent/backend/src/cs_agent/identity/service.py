import hmac
import secrets
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from cs_agent.audit.service import AuditIdentity, record
from cs_agent.core.context import Role, TrustedContext
from cs_agent.core.errors import AppError
from cs_agent.db.models import LoginSession, RoleGrant, SourceBatch, Store, Subject
from cs_agent.identity.security import csrf_for, digest, hash_password, verify_password

DUMMY_PASSWORD = hash_password(secrets.token_urlsafe(24))


def roles_for(session, subject, now):
    if subject.subject_type == "customer":
        return [Role.CUSTOMER]
    rows = session.scalars(
        select(RoleGrant.role).where(
            RoleGrant.staff_id == subject.subject_id,
            RoleGrant.store_id == subject.store_id,
            RoleGrant.grant_status == "active",
            RoleGrant.data_batch_id == subject.data_batch_id,
            RoleGrant.batch_generation == subject.batch_generation,
            RoleGrant.valid_from <= now,
            or_(RoleGrant.valid_to.is_(None), RoleGrant.valid_to > now),
        )
    )
    return sorted({Role(role) for role in rows})


def check_subject(session, subject):
    if subject is None or subject.account_status != "active":
        raise AppError("unauthenticated", 401, "请重新登录")
    store = session.get(Store, subject.store_id)
    batch = session.get(SourceBatch, subject.data_batch_id)
    if (
        store is None
        or store.status != "active"
        or batch is None
        or batch.status != "active"
        or batch.generation != subject.batch_generation
    ):
        raise AppError("unauthenticated", 401, "请重新登录")


def login(session, request, login_name, password):
    subject = session.scalar(select(Subject).where(Subject.login_name == login_name))
    valid = verify_password(password, subject.password_digest if subject else DUMMY_PASSWORD)
    if subject is None or not valid:
        raise AppError("unauthenticated", 401, "账号或密码无效")
    check_subject(session, subject)
    now = request.app.state.clock.now()
    roles = roles_for(session, subject, now)
    if not roles:
        raise AppError("unauthenticated", 401, "账号或密码无效")
    old = session.scalar(
        select(LoginSession)
        .where(LoginSession.token_digest == digest(request.cookies.get("cs_session", "")))
        .with_for_update()
    )
    if old and old.status == "active":
        old.status, old.revoked_at = "revoked", now
    token = secrets.token_urlsafe(32)
    current = LoginSession(
        session_id="ses_" + uuid4().hex,
        subject_id=subject.subject_id,
        token_digest=digest(token),
        csrf_digest=digest(csrf_for(token)),
        active_role=roles[0].value,
        auth_version=subject.auth_version,
        status="active",
        expires_at=now + timedelta(seconds=request.app.state.settings.session_seconds),
        source_kind="derived",
        data_batch_id=subject.data_batch_id,
        batch_generation=subject.batch_generation,
    )
    session.add(current)
    session.flush()
    ctx = TrustedContext(
        subject.subject_id,
        subject.store_id,
        roles[0],
        current.session_id,
        subject.auth_version,
        subject.data_batch_id,
        subject.batch_generation,
        request.state.request_id,
    )
    request.state.ctx = ctx
    record(
        session,
        request_id=ctx.request_id,
        action="session.login",
        outcome="succeeded",
        ctx=ctx,
        summary={"reason_code": "session_created", "auth_version": ctx.auth_version},
    )
    return ctx, current, roles, token


def authenticate(session, request):
    token = request.cookies.get("cs_session", "")
    if len(token) > 200:
        raise AppError("unauthenticated", 401, "请重新登录")
    current = session.scalar(
        select(LoginSession).where(LoginSession.token_digest == digest(token)).with_for_update()
    )
    now = request.app.state.clock.now()
    if current is None:
        raise AppError("unauthenticated", 401, "请重新登录")
    subject = session.get(Subject, current.subject_id)
    if subject:
        request.state.audit_identity = AuditIdentity(
            subject.subject_id,
            subject.store_id,
            Role(current.active_role),
            current.auth_version,
            current.data_batch_id,
            current.batch_generation,
        )
    if current.status != "active" or current.expires_at <= now:
        raise AppError("unauthenticated", 401, "请重新登录")
    check_subject(session, subject)
    roles = roles_for(session, subject, now)
    if (
        current.auth_version != subject.auth_version
        or Role(current.active_role) not in roles
        or current.data_batch_id != subject.data_batch_id
        or current.batch_generation != subject.batch_generation
    ):
        raise AppError("unauthenticated", 401, "请重新登录")
    ctx = TrustedContext(
        subject.subject_id,
        subject.store_id,
        Role(current.active_role),
        current.session_id,
        subject.auth_version,
        subject.data_batch_id,
        subject.batch_generation,
        request.state.request_id,
    )
    request.state.ctx = ctx
    return ctx, current, subject, roles


def require_origin(request):
    if request.headers.get("origin") != request.app.state.settings.browser_origin:
        raise AppError("forbidden", 403, "请求来源无效")


def require_csrf(request, current):
    require_origin(request)
    token = request.headers.get("x-csrf-token", "")
    if len(token) != 64 or not hmac.compare_digest(digest(token), current.csrf_digest):
        raise AppError("forbidden", 403, "请求校验失败")


def session_view(request, ctx, current, roles, session):
    from cs_agent.identity.operations import operations_for

    projection = operations_for(session, ctx, request.app.state.clock.now())
    return {
        "subject_id": ctx.subject_id,
        "store_id": ctx.store_id,
        "active_role": ctx.role,
        "roles": roles,
        "auth_version": ctx.auth_version,
        "capabilities": list(projection.capabilities),
        "capability_revision": projection.capability_revision,
        "expires_at": current.expires_at.isoformat(),
        "csrf_token": csrf_for(request.cookies.get("cs_session", "")),
    }
