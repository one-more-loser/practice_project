import asyncio
import json
import time
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import Field
from sqlalchemy import select

from cs_agent.conversation.identity import create_guest, principal_for
from cs_agent.conversation.service import ConversationService
from cs_agent.core.errors import AppError
from cs_agent.core.types import ContractModel
from cs_agent.identity.api import DB
from cs_agent.identity.security import csrf_for
from cs_agent.main import envelope

router = APIRouter(prefix="/api/v1")
service = ConversationService()


class Empty(ContractModel):
    pass


class MessageInput(ContractModel):
    client_message_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_:-]+$")
    text: str = Field(min_length=1, max_length=4000)


class CloseInput(ContractModel):
    expected_version: int = Field(ge=1)


@router.get("/conversations/{ref}/action-proposals")
def pending_proposals(ref: str, request: Request, session: DB):
    from cs_agent.conversation.service import owned
    from cs_agent.db.agent_action_tables import bindings
    from cs_agent.db.business_tables import confirmations

    principal = principal_for(session, request)
    row = owned(session, principal, ref)
    if row["mode"] != "agent":
        return envelope(request, {"items": []})
    items = (
        session.execute(
            select(
                confirmations.c.confirmation_id, confirmations.c.summary, confirmations.c.expires_at
            )
            .join(bindings, bindings.c.confirmation_id == confirmations.c.confirmation_id)
            .where(
                bindings.c.conversation_id == ref,
                bindings.c.mode_epoch == row["mode_epoch"],
                confirmations.c.status == "pending",
                confirmations.c.expires_at > request.app.state.clock.now(),
            )
            .order_by(confirmations.c.created_at.desc())
            .limit(3)
        )
        .mappings()
        .all()
    )
    return envelope(request, {"items": [dict(item) for item in items]})


@router.post("/guest-session", status_code=201)
def new_guest(request: Request, response: Response, session: DB):
    return envelope(request, create_guest(session, request, response))


@router.get("/guest-session")
def guest(request: Request, session: DB):
    principal = principal_for(session, request)
    token = request.cookies.get("cs_session") or request.cookies.get("cs_guest", "")
    return envelope(request, {"kind": principal.kind, "csrf_token": csrf_for(token)})


@router.post("/conversations", status_code=201)
def create(body: Empty, request: Request, session: DB):
    return envelope(
        request,
        service.create(
            session, principal_for(session, request, True), request.app.state.clock.now()
        ),
    )


@router.get("/conversations/{ref}")
def get(ref: str, request: Request, session: DB):
    return envelope(request, service.get(session, principal_for(session, request), ref))


@router.post("/conversations/{ref}/messages", status_code=202)
def accept(ref: str, body: MessageInput, request: Request, session: DB):
    if (
        request.app.state.settings.agent_worker_enabled
        and request.app.state.agent_runtime != "ready"
    ):
        raise AppError("unavailable", 503, "客服处理服务尚未就绪，请稍后重试")
    return envelope(
        request,
        service.accept_message(
            session,
            principal_for(session, request, True),
            ref,
            body.client_message_id,
            body.text,
            request.app.state.clock.now(),
        ),
    )


@router.get("/conversations/{ref}/messages")
def history(
    ref: str, request: Request, session: DB, limit: Annotated[int, Query(ge=1, le=50)] = 20
):
    return envelope(
        request, {"items": service.history(session, principal_for(session, request), ref, limit)}
    )


@router.get("/runs/{run_id}")
def run(run_id: str, request: Request, session: DB):
    row, _ = service.run(session, principal_for(session, request), run_id)
    return envelope(
        request, {key: row[key] for key in ("run_id", "status", "context_kind", "error_code")}
    )


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: str, body: Empty, request: Request, session: DB):
    return envelope(
        request,
        service.cancel(
            session, principal_for(session, request, True), run_id, request.app.state.clock.now()
        ),
    )


@router.post("/conversations/{ref}/close")
def close(ref: str, body: CloseInput, request: Request, session: DB):
    return envelope(
        request,
        service.close(
            session,
            principal_for(session, request, True),
            ref,
            body.expected_version,
            request.app.state.clock.now(),
        ),
    )


@router.get("/conversations/{ref}/events")
async def stream(
    ref: str, request: Request, after_seq: Annotated[int, Query(ge=0)] = 0, follow: bool = True
):
    # Last-Event-ID优先；鉴权与游标在发送SSE响应头之前完成。
    cursor = request.headers.get("last-event-id", str(after_seq))
    if not cursor.isdecimal() or len(cursor) > 12:
        raise AppError("invalid_cursor", 422, "事件游标不合法")
    cursor = int(cursor)
    db = request.app.state.db
    if db is None:
        raise AppError("unavailable", 503, "数据库暂不可用")

    def read(after):
        with db.transaction() as session:
            return service.replay(session, principal_for(session, request), ref, after)

    initial = await asyncio.to_thread(read, cursor)

    async def generate():
        nonlocal cursor
        batch = initial
        heartbeat = time.monotonic()
        while True:
            for event in batch:
                cursor = event["seq"]
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if not follow or await request.is_disconnected():
                break
            if time.monotonic() - heartbeat >= 15:
                yield ": heartbeat\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(0.5)
            try:
                batch = await asyncio.to_thread(read, cursor)
            except AppError as exc:
                yield "event: stream.reset\ndata: " + json.dumps({"code": exc.code}) + "\n\n"
                break

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
