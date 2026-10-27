import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from cs_agent.actions.repository import new_id
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError
from cs_agent.db.models import SourceBatch, Store
from cs_agent.db.runtime_tables import guest_sessions
from cs_agent.identity.security import csrf_for, digest
from cs_agent.identity.service import authenticate, require_csrf, require_origin


@dataclass(frozen=True)
class Principal:
    owner_id: str
    kind: str
    store_id: str
    data_batch_id: str
    batch_generation: int
    session_ref: str
    auth_version: int
    context: object = None


def common(principal, now):
    return {
        "store_id": principal.store_id,
        "source_kind": "derived",
        "data_batch_id": principal.data_batch_id,
        "batch_generation": principal.batch_generation,
        "created_at": now,
        "updated_at": now,
        "version": 1,
    }


def create_guest(session, request, response):
    require_origin(request)
    now = request.app.state.clock.now()
    store = session.get(Store, "store_sim")
    if store is None or store.status != "active":
        raise AppError("unavailable", 503, "店铺暂不可用")
    batch = session.get(SourceBatch, store.data_batch_id)
    if batch is None or batch.status != "active" or batch.generation != store.batch_generation:
        raise AppError("unavailable", 503, "店铺暂不可用")
    token = secrets.token_urlsafe(32)
    principal = Principal(
        new_id("guest"), "guest", store.store_id, store.data_batch_id, store.batch_generation, "", 1
    )
    session.execute(
        guest_sessions.insert().values(
            guest_id=principal.owner_id,
            token_digest=digest(token),
            expires_at=now + timedelta(hours=24),
            status="active",
            **common(principal, now),
        )
    )
    response.set_cookie(
        "cs_guest",
        token,
        max_age=86400,
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"kind": "guest", "csrf_token": csrf_for(token)}


def principal_for(session, request, mutating=False):
    if request.cookies.get("cs_session"):
        ctx, current, _, _ = authenticate(session, request)
        if ctx.role != Role.CUSTOMER:
            raise AppError("forbidden", 403, "请使用顾客身份访问此入口")
        if mutating:
            require_csrf(request, current)
        return Principal(
            ctx.subject_id,
            "customer",
            ctx.store_id,
            ctx.data_batch_id,
            ctx.batch_generation,
            ctx.session_ref,
            ctx.auth_version,
            ctx,
        )
    token = request.cookies.get("cs_guest", "")
    row = (
        session.execute(
            select(guest_sessions).where(guest_sessions.c.token_digest == digest(token))
        )
        .mappings()
        .first()
    )
    now = request.app.state.clock.now()
    if not row or row["status"] != "active" or row["expires_at"] <= now:
        raise AppError("unauthenticated", 401, "请建立新的服务会话")
    store = session.get(Store, row["store_id"])
    batch = session.get(SourceBatch, row["data_batch_id"])
    if (
        not store
        or store.status != "active"
        or not batch
        or batch.status != "active"
        or batch.generation != row["batch_generation"]
    ):
        raise AppError("unauthenticated", 401, "请建立新的服务会话")
    if mutating:
        require_origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf_for(token)):
            raise AppError("forbidden", 403, "请求校验失败")
    return Principal(
        row["guest_id"],
        "guest",
        row["store_id"],
        row["data_batch_id"],
        row["batch_generation"],
        row["guest_id"],
        1,
    )
