from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs_agent.audit.service import record
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.core.types import ContractModel
from cs_agent.db.models import ShippingInput
from cs_agent.db.session import session_dependency
from cs_agent.identity.policies import ResourceScope, require_owner
from cs_agent.identity.service import (
    authenticate,
    login,
    require_csrf,
    require_origin,
    session_view,
)
from cs_agent.main import envelope
from cs_agent.privacy.projections import PrivateContact, personal_contact

router = APIRouter(prefix="/api/v1")
DB = Annotated[Session, Depends(session_dependency, scope="function")]


class LoginInput(ContractModel):
    login_name: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_]+$")
    password: SecretStr = Field(min_length=1, max_length=256)


class SwitchRole(ContractModel):
    role: Role


@router.post("/session")
def create_session(body: LoginInput, request: Request, response: Response, session: DB):
    require_origin(request)
    ctx, current, roles, token = login(
        session, request, body.login_name, body.password.get_secret_value()
    )
    settings = request.app.state.settings
    response.set_cookie(
        "cs_session",
        token,
        max_age=settings.session_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    from cs_agent.identity.security import csrf_for

    result = session_view(request, ctx, current, roles, session)
    result["csrf_token"] = csrf_for(token)
    return envelope(request, result)


@router.get("/session")
def current_session(request: Request, session: DB):
    ctx, current, subject, roles = authenticate(session, request)
    return envelope(request, session_view(request, ctx, current, roles, session))


@router.delete("/session")
def logout(request: Request, response: Response, session: DB):
    ctx, current, subject, roles = authenticate(session, request)
    require_csrf(request, current)
    current.status, current.revoked_at = "revoked", request.app.state.clock.now()
    record(
        session,
        request_id=ctx.request_id,
        action="session.logout",
        outcome="succeeded",
        ctx=ctx,
        summary={"reason_code": "session_revoked"},
    )
    response.delete_cookie(
        "cs_session",
        path="/",
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="strict",
    )
    return envelope(request, {"status": "revoked"})


@router.patch("/session/role")
def switch_role(body: SwitchRole, request: Request, session: DB):
    ctx, current, subject, roles = authenticate(session, request)
    require_csrf(request, current)
    if body.role not in roles:
        raise AppError("forbidden", 403, "当前角色不允许此操作")
    old_role = ctx.role
    current.active_role, current.version = body.role.value, current.version + 1
    ctx = replace(ctx, role=body.role)
    request.state.ctx = ctx
    record(
        session,
        request_id=ctx.request_id,
        action="session.role.switched",
        outcome="succeeded",
        ctx=ctx,
        summary={
            "reason_code": "role_switched",
            "from_role": old_role.value,
            "to_role": ctx.role.value,
            "version": current.version,
        },
    )
    return envelope(request, session_view(request, ctx, current, roles, session))


@router.get("/me/shipping-inputs/{input_id}")
def read_personal_input(input_id: str, request: Request, session: DB):
    ctx, current, subject, roles = authenticate(session, request)
    # 本人范围直接进入数据库查询条件，不能先向服务层/模型返回他人原始记录再遮盖。
    require_owner(ctx, ResourceScope(ctx.store_id, ctx.subject_id))
    row = session.scalar(
        select(ShippingInput).where(
            ShippingInput.input_id == input_id,
            ShippingInput.owner_id == ctx.subject_id,
            ShippingInput.store_id == ctx.store_id,
            ShippingInput.data_batch_id == ctx.data_batch_id,
            ShippingInput.batch_generation == ctx.batch_generation,
        )
    )
    if (
        row is None
        or row.data_batch_id != ctx.data_batch_id
        or row.batch_generation != ctx.batch_generation
    ):
        raise not_found()
    require_owner(ctx, ResourceScope(row.store_id, row.owner_id))
    contact = PrivateContact(
        row.input_id,
        row.recipient,
        row.phone,
        row.province,
        row.city,
        row.district,
        row.street,
        row.postal_code,
    )
    record(
        session,
        request_id=ctx.request_id,
        action="personal.shipping.read",
        outcome="allowed",
        ctx=ctx,
        resource_ref=row.input_id,
        summary={"reason_code": "personal_read"},
    )
    return envelope(request, personal_contact(contact))
