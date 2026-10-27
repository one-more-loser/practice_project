import hmac
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import Field, StrictInt
from sqlalchemy.exc import OperationalError

from cs_agent.actions.repository import execute_once
from cs_agent.activities import service
from cs_agent.activities.client import OperationsClient
from cs_agent.activities.repository import receipt
from cs_agent.core.errors import AppError
from cs_agent.core.types import ContractModel
from cs_agent.identity.operations import require_operations
from cs_agent.identity.service import authenticate, require_csrf

router = APIRouter(prefix="/api/v1")


class PreviewInput(ContractModel):
    contract_version: Literal["XOPS-ACTIVITY-v0.1"]
    store_id: str = Field(min_length=1, max_length=80)
    plan_id: str = Field(min_length=1, max_length=100)
    plan_version: StrictInt = Field(gt=0)
    manifest_digest: str = Field(pattern="^[a-f0-9]{64}$")
    manifest: dict[str, Any]


class PublishInput(ContractModel):
    plan_id: str = Field(min_length=1, max_length=100)
    plan_version: StrictInt = Field(gt=0)
    approval_ref: str = Field(min_length=1, max_length=100)
    manifest_digest: str = Field(pattern="^[a-f0-9]{64}$")
    preview_ref: str = Field(min_length=1, max_length=100)


class ManageInput(ContractModel):
    expected_revision: StrictInt = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000, pattern=r"\S")


class CancelInput(ContractModel):
    actor_ref: str = Field(min_length=1, max_length=100)
    auth_version: StrictInt = Field(gt=0)
    capability_revision: StrictInt = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    grant_ref: str = Field(min_length=1, max_length=100)


def envelope(request, data):
    return dict(
        contract_version="API-CS-v0.1",
        request_id=getattr(request.state, "request_id", ""),
        data=data,
    )


def db_for(request):
    db = request.app.state.db
    if db is None:
        raise AppError("unavailable", 503, "数据库尚未就绪")
    return db


def auth(session, request, capability, *, write=False):
    session.info["clock"] = request.app.state.clock
    ctx, current, _, _ = authenticate(session, request)
    if write:
        require_csrf(request, current)
    require_operations(session, ctx, capability, request.app.state.clock.now())
    return ctx


@router.post("/shop/activity-previews")
def create_preview(body: PreviewInput, request: Request):
    with db_for(request).transaction() as session:
        ctx = auth(session, request, "activity.publish", write=True)
        data = service.preview(session, ctx, body.model_dump(), request.app.state.clock.now())
    return envelope(request, data)


@router.post("/shop/activities")
def publish_activity(body: PublishInput, request: Request):
    db = db_for(request)
    now = request.app.state.clock.now
    payload = body.model_dump()
    key = request.headers.get("Idempotency-Key")
    with db.transaction() as session:
        ctx = auth(session, request, "activity.publish", write=True)
        original = service.find_slot(session, ctx, payload)
        if original and original["status"] != "pending":
            execute_once(
                session,
                ctx,
                "activity.publish.slot",
                key,
                payload,
                now(),
                lambda _: dict(intent_id=original["intent_id"]),
            )
            return envelope(request, receipt(session, original, now()))
    # 没有数据库事务/连接锁跨越远程请求。
    client = OperationsClient(request.app.state.settings, ctx.store_id)
    revision = None if original else client.revision(body.plan_id, ctx.store_id)
    with db.transaction() as session:
        # re-auth Cookie，不能把第一个事务的会话快照当作当前授权。
        ctx = auth(session, request, "activity.publish", write=True)
        slot = service.prepare_slot(session, ctx, payload, revision, now())
        execute_once(
            session,
            ctx,
            "activity.publish.slot",
            key,
            payload,
            now(),
            lambda _: dict(intent_id=slot["intent_id"]),
        )
        if slot["status"] != "pending":
            return envelope(request, receipt(session, slot, now()))
    try:
        grant = client.grant(service.grant_request(slot))
        with db.transaction() as session:
            service.remember_grant(session, ctx, slot["intent_id"], grant, now())
        with db.transaction() as session:
            ctx = auth(session, request, "activity.publish", write=True)
            data = service.commit_grant(
                session, ctx, slot["intent_id"], grant, now(), clock=request.app.state.clock
            )
        return envelope(request, data)
    except OperationalError:
        return JSONResponse(
            envelope(request, dict(status="unknown", intent_id=slot["intent_id"], retryable=True)),
            status_code=202,
        )
    except AppError as exc:
        if exc.status in {422, 503}:
            return JSONResponse(
                envelope(
                    request, dict(status="unknown", intent_id=slot["intent_id"], retryable=True)
                ),
                status_code=202,
            )
        raise


@router.get("/shop/activities")
def list_activities(request: Request):
    with db_for(request).transaction() as session:
        ctx = auth(session, request, "metrics.read")
        data = service.list_activities(session, ctx, request.app.state.clock.now())
    return envelope(request, data)


@router.get("/shop/activities/{activity_id}")
def activity_detail(activity_id: str, request: Request):
    with db_for(request).transaction() as session:
        ctx = auth(session, request, "metrics.read")
        data = service.list_activities(session, ctx, request.app.state.clock.now(), activity_id)
    return envelope(request, data)


@router.post("/shop/activities/{activity_id}/{action}")
def manage_activity(
    activity_id: str, action: Literal["pause", "resume", "end"], body: ManageInput, request: Request
):
    with db_for(request).transaction() as session:
        ctx = auth(session, request, "activity.manage", write=True)
        data, status = service.manage(
            session,
            ctx,
            activity_id,
            action,
            body.model_dump(),
            request.headers.get("Idempotency-Key"),
            request.app.state.clock.now(),
        )
    return JSONResponse(envelope(request, data), status_code=status)


def service_auth(request, store_id):
    settings = request.app.state.settings
    token = getattr(settings, "activity_service_token", None)
    token = token.get_secret_value() if hasattr(token, "get_secret_value") else token
    stores = getattr(settings, "activity_service_stores", ())
    if not token or not stores:
        raise AppError("activity_service_unavailable", 503, "活动服务尚未配置")
    if not hmac.compare_digest(
        request.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()
    ):
        raise AppError("unauthenticated", 401, "服务身份无效")
    if store_id not in stores:
        raise AppError("not_found", 404, "资源不存在或不可访问")


def internal(request, store_id, intent_id, action=None, body=None):
    service_auth(request, store_id)
    try:
        with db_for(request).transaction() as session:
            data = service.internal_result(
                session,
                store_id,
                intent_id,
                request.app.state.clock.now(),
                action,
                body,
                clock=request.app.state.clock,
            )
        return envelope(request, data)
    except OperationalError:
        return envelope(request, dict(status="unknown"))


@router.get("/internal/stores/{store_id}/execution-intents/{intent_id}")
def intent_detail(store_id: str, intent_id: str, request: Request):
    return internal(request, store_id, intent_id)


@router.post("/internal/stores/{store_id}/execution-intents/{intent_id}/finalize-expired")
def finalize_intent(store_id: str, intent_id: str, request: Request):
    return internal(request, store_id, intent_id, "finalize-expired")


@router.post("/internal/stores/{store_id}/execution-intents/{intent_id}/cancel-request")
def cancel_intent(store_id: str, intent_id: str, body: CancelInput, request: Request):
    return internal(request, store_id, intent_id, "cancel-request", body.model_dump())
