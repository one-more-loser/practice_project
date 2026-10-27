from datetime import datetime
from hmac import compare_digest
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field

from cs_agent.business_api import DB, context
from cs_agent.conversation.identity import principal_for
from cs_agent.core.errors import AppError
from cs_agent.core.types import ContractModel
from cs_agent.feedback.aggregation import FeedbackService
from cs_agent.main import envelope

router = APIRouter(prefix="/api/v1")
service = FeedbackService()


class Feedback(ContractModel):
    message_id: str = Field(min_length=1, max_length=80)
    rating: Literal["helpful", "unhelpful"]
    topics: list[str] = Field(min_length=1, max_length=7)
    comment: str = Field(default="", max_length=1000)


class Window(ContractModel):
    week_start: datetime


@router.post("/conversations/{ref}/feedback")
def receive(ref: str, body: Feedback, request: Request, session: DB):
    principal = principal_for(session, request, True)
    if principal.kind != "customer":
        raise AppError("forbidden", 403, "请登录后提交反馈")
    return envelope(
        request,
        service.receive(session, principal, ref, body.model_dump(), request.app.state.clock.now()),
    )


@router.post("/staff/support-summary")
def build(body: Window, request: Request, session: DB):
    return envelope(
        request,
        service.build(
            session, context(session, request, True), body.week_start, request.app.state.clock.now()
        ),
    )


@router.get("/internal/stores/{store_id}/support-summary")
def read(store_id: str, week_start: datetime, request: Request, session: DB):
    # 共享契约草案的只读服务入口；未配置专用密钥/店铺绑定时关闭。
    settings = request.app.state.settings
    if not settings.operations_api_key or not settings.operations_store_id:
        raise AppError("unavailable", 503, "共享服务尚未配置")
    supplied = request.headers.get("Authorization", "")
    expected = "Bearer " + settings.operations_api_key.get_secret_value()
    if not compare_digest(supplied.encode("utf8"), expected.encode("utf8")):
        raise AppError("unauthenticated", 401, "服务身份无效")
    if store_id != settings.operations_store_id:
        raise AppError("not_found", 404, "资源不存在或不可访问")
    return envelope(
        request, service.read(session, store_id, week_start, request.app.state.clock.now())
    )
