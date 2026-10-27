import asyncio
import json
from time import monotonic
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from cs_agent.conversation.identity import principal_for
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError
from cs_agent.core.types import ContractModel
from cs_agent.db.models import Subject
from cs_agent.db.session import session_dependency
from cs_agent.identity.service import authenticate, require_csrf, require_origin, roles_for
from cs_agent.main import envelope
from cs_agent.support.human_service import HumanService
from cs_agent.support.service import TicketService, principal_from

router = APIRouter(prefix="/api/v1")
DB = Annotated[Session, Depends(session_dependency, scope="function")]


def context(session, request, mutating=False):
    ctx, current, _, _ = authenticate(session, request)
    if mutating:
        require_csrf(request, current)
    return ctx


class VersionInput(ContractModel):
    expected_version: int = Field(ge=1, strict=True)


class Reopen(VersionInput):
    reason: str = Field(min_length=1, max_length=500)


class Assignment(Reopen):
    staff_id: str = Field(min_length=1, max_length=80)


class TicketAction(VersionInput):
    action: Literal["accept", "reply", "note", "wait", "resolve", "close", "escalate"]
    text: str = Field(default="", max_length=4000)


class HumanRequest(VersionInput):
    expected_epoch: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=500)


class HumanCancel(VersionInput):
    expected_epoch: int = Field(ge=1, strict=True)


class HumanTakeover(HumanCancel):
    assignment_id: str = Field(min_length=1, max_length=80)
    assignment_version: int = Field(ge=1, strict=True)


class HumanEnd(HumanTakeover):
    reason: str = Field(min_length=1, max_length=500)


class HumanReply(ContractModel):
    client_message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    assignment_version: int = Field(ge=1, strict=True)
    expected_epoch: int = Field(ge=1, strict=True)


@router.get("/me/tickets")
def own_tickets(request: Request, session: DB):
    return envelope(
        request,
        {
            "items": TicketService().list_owned(
                session, context(session, request), request.app.state.clock.now()
            )
        },
    )


@router.get("/me/tickets/{ticket_id}")
def own_ticket(ticket_id: str, request: Request, session: DB):
    return envelope(
        request,
        TicketService().get(
            session, context(session, request), ticket_id, request.app.state.clock.now()
        ),
    )


@router.post("/me/tickets/{ticket_id}/reopen")
def reopen(ticket_id: str, body: Reopen, request: Request, session: DB):
    return envelope(
        request,
        TicketService().reopen(
            session,
            context(session, request, True),
            ticket_id,
            body.reason,
            body.expected_version,
            request.app.state.clock.now(),
        ),
    )


@router.get("/staff/queue")
def queue(request: Request, session: DB):
    return envelope(
        request,
        {
            "items": TicketService().queue(
                session, context(session, request), request.app.state.clock.now()
            )
        },
    )


@router.get("/staff/assignees")
def assignees(request: Request, session: DB):
    ctx = context(session, request)
    if ctx.role != Role.SUPERVISOR:
        raise AppError("forbidden", 403, "仅主管可以分派")
    rows = session.scalars(
        select(Subject).where(
            Subject.store_id == ctx.store_id,
            Subject.data_batch_id == ctx.data_batch_id,
            Subject.batch_generation == ctx.batch_generation,
            Subject.account_status == "active",
            Subject.subject_type == "staff",
        )
    ).all()
    return envelope(
        request,
        {
            "items": [
                {"staff_id": row.subject_id, "name": row.login_name}
                for row in rows
                if Role.SUPPORT in roles_for(session, row, request.app.state.clock.now())
            ]
        },
    )


@router.get("/staff/tickets/{ticket_id}")
def staff_ticket(ticket_id: str, request: Request, session: DB):
    return envelope(
        request,
        TicketService().get(
            session, context(session, request), ticket_id, request.app.state.clock.now(), staff=True
        ),
    )


@router.post("/staff/tickets/{ticket_id}/assignments")
def assign(ticket_id: str, body: Assignment, request: Request, session: DB):
    return envelope(
        request,
        TicketService().assign(
            session,
            context(session, request, True),
            ticket_id,
            body.staff_id,
            body.reason,
            body.expected_version,
            request.app.state.clock.now(),
        ),
    )


@router.post("/staff/tickets/{ticket_id}/actions")
def ticket_action(ticket_id: str, body: TicketAction, request: Request, session: DB):
    return envelope(
        request,
        TicketService().act(
            session,
            context(session, request, True),
            ticket_id,
            body.action,
            body.text,
            body.expected_version,
            request.app.state.clock.now(),
        ),
    )


@router.post("/conversations/{ref}/human-requests")
def human_request(ref: str, body: HumanRequest, request: Request, session: DB):
    return envelope(
        request,
        HumanService().request(
            session,
            principal_for(session, request, True),
            ref,
            body.reason,
            body.expected_epoch,
            body.expected_version,
            request.app.state.clock.now(),
        ),
    )


@router.post("/conversations/{ref}/cancel-human-request")
def human_cancel(ref: str, body: HumanCancel, request: Request, session: DB):
    return envelope(
        request,
        HumanService().cancel_request(
            session,
            principal_for(session, request, True),
            ref,
            body.expected_epoch,
            body.expected_version,
            request.app.state.clock.now(),
        ),
    )


@router.post("/staff/conversations/{ref}/takeover")
def takeover(ref: str, body: HumanTakeover, request: Request, session: DB):
    return envelope(
        request,
        HumanService().takeover(
            session,
            context(session, request, True),
            ref,
            body.assignment_id,
            body.assignment_version,
            body.expected_epoch,
            body.expected_version,
            request.app.state.clock.now(),
        ),
    )


@router.post("/staff/conversations/{ref}/end-service")
def end(ref: str, body: HumanEnd, request: Request, session: DB):
    return envelope(
        request,
        HumanService().end(
            session,
            context(session, request, True),
            ref,
            body.assignment_id,
            body.assignment_version,
            body.expected_epoch,
            body.expected_version,
            body.reason,
            request.app.state.clock.now(),
        ),
    )


@router.get("/staff/conversations/{ref}")
def staff_conversation(ref: str, request: Request, session: DB):
    return envelope(request, HumanService().history(session, context(session, request), ref))


@router.post("/staff/conversations/{ref}/messages")
def staff_reply(ref: str, body: HumanReply, request: Request, session: DB):
    return envelope(
        request,
        HumanService().staff_reply(
            session,
            context(session, request, True),
            ref,
            body.client_message_id,
            body.text,
            body.assignment_version,
            body.expected_epoch,
            request.app.state.clock.now(),
        ),
    )


def ws_request(socket, csrf=None):
    headers = list(socket.scope["headers"])
    if csrf is not None:
        headers = [(k, v) for k, v in headers if k.lower() != b"x-csrf-token"]
        headers.append((b"x-csrf-token", csrf.encode()))
    request = Request(
        {**socket.scope, "type": "http", "method": "GET", "headers": headers, "state": {}}
    )
    request.state.request_id = "ws_" + uuid4().hex
    return request


def ws_identity(session, socket, csrf=None):
    request = ws_request(socket, csrf)
    ctx, current, _, _ = authenticate(session, request)
    if csrf is not None:
        require_csrf(request, current)
    if ctx.role not in {Role.CUSTOMER, Role.SUPPORT, Role.SUPERVISOR}:
        raise AppError("forbidden", 403, "当前角色不能访问人工服务")
    return principal_from(ctx) if ctx.role == Role.CUSTOMER else ctx


def ws_transaction(socket, operation, csrf=None):
    db = socket.app.state.db
    if db is None:
        raise AppError("unavailable", 503, "数据库尚未就绪")
    with db.transaction() as session:
        identity = ws_identity(session, socket, csrf)
        return operation(session, identity)


def subscription(value):
    if not isinstance(value, dict):
        raise AppError("invalid_input", 422, "订阅格式无效")
    ref, after, epoch = (
        value.get("conversation_id"),
        value.get("after_seq", 0),
        value.get("expected_epoch"),
    )
    if (
        not isinstance(ref, str)
        or not 1 <= len(ref) <= 80
        or type(after) is not int
        or after < 0
        or type(epoch) is not int
        or epoch < 1
    ):
        raise AppError("invalid_input", 422, "订阅参数无效")
    return ref, {"after_seq": after, "expected_epoch": epoch, "delivered_seq": after}


async def read_frame(socket, timeout):
    raw = await asyncio.wait_for(socket.receive_text(), timeout)
    if len(raw) > 30000:
        raise AppError("invalid_input", 422, "消息过长")
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise AppError("invalid_input", 422, "消息格式无效") from exc
    if not isinstance(value, dict):
        raise AppError("invalid_input", 422, "消息格式无效")
    return value


@router.websocket("/realtime")
async def realtime(socket: WebSocket):
    try:
        require_origin(socket)
    except AppError:
        await socket.close(code=4403)
        return
    await socket.accept()
    subscriptions = {}
    try:
        hello = await read_frame(socket, 5)
        csrf = hello.get("csrf_token")
        requested = hello.get("subscriptions", [])
        if (
            hello.get("type") != "hello"
            or not isinstance(csrf, str)
            or len(csrf) != 64
            or not isinstance(requested, list)
            or len(requested) > 5
        ):
            raise AppError("forbidden", 403, "请先完成会话校验")
        await run_in_threadpool(ws_transaction, socket, lambda session, identity: None, csrf)
        for value in requested:
            ref, state = subscription(value)
            await run_in_threadpool(
                ws_transaction,
                socket,
                lambda session, identity, ref=ref: HumanService().authorized_conversation(
                    session, identity, ref
                ),
            )
            subscriptions[ref] = state
        await socket.send_json({"type": "ready", "heartbeat_seconds": 15})
        last_ping, last_input, last_poll = monotonic(), monotonic(), 0.0
        while True:
            frame = None
            try:
                frame = await read_frame(socket, 0.5)
                last_input = monotonic()
            except TimeoutError:
                pass
            if frame:
                kind = frame.get("type")
                if kind in {"subscribe", "resume"}:
                    ref, state = subscription(frame)
                    if ref not in subscriptions and len(subscriptions) >= 5:
                        raise AppError("invalid_input", 422, "订阅数量超限")
                    await run_in_threadpool(
                        ws_transaction,
                        socket,
                        lambda session, identity: HumanService().authorized_conversation(
                            session, identity, ref
                        ),
                    )
                    subscriptions[ref] = state
                elif kind == "staff.reply":
                    ref = frame.get("conversation_id")
                    body = HumanReply.model_validate(
                        {
                            key: frame.get(key)
                            for key in (
                                "client_message_id",
                                "text",
                                "assignment_version",
                                "expected_epoch",
                            )
                        }
                    )

                    def save_reply(session, identity):
                        if hasattr(identity, "owner_id"):
                            raise AppError("forbidden", 403, "仅获分派客服可以回复")
                        return HumanService().staff_reply(
                            session,
                            identity,
                            ref,
                            body.client_message_id,
                            body.text,
                            body.assignment_version,
                            body.expected_epoch,
                            socket.app.state.clock.now(),
                        )

                    result = await run_in_threadpool(ws_transaction, socket, save_reply)
                    await socket.send_json(
                        {"type": "ack", "client_message_id": body.client_message_id, **result}
                    )
                elif kind == "delivery.ack":
                    state = subscriptions.get(frame.get("conversation_id"))
                    seq = frame.get("seq")
                    if not state or type(seq) is not int or seq < 0 or seq > state["delivered_seq"]:
                        raise AppError("invalid_input", 422, "投递位置无效")
                    await run_in_threadpool(ws_transaction, socket, lambda session, identity: None)
                elif kind == "ping":
                    await run_in_threadpool(ws_transaction, socket, lambda session, identity: None)
                    await socket.send_json({"type": "pong"})
                elif kind == "pong":
                    pass
                else:
                    raise AppError("invalid_input", 422, "消息类型无效")
            now_tick = monotonic()
            if now_tick - last_poll >= 1:
                last_poll = now_tick
                # PostgreSQL持久流是唯一来源；进程间无需共享连接或内存广播表。
                for ref, state in list(subscriptions.items()):
                    row, items, cursor = await run_in_threadpool(
                        ws_transaction,
                        socket,
                        lambda session, identity, ref=ref, state=state: HumanService().replay(
                            session, identity, ref, state["after_seq"], state["expected_epoch"]
                        ),
                    )
                    if row["mode_epoch"] != state["expected_epoch"]:
                        await socket.send_json(
                            {
                                "type": "mode.sync",
                                "conversation_id": ref,
                                "mode": row["mode"],
                                "mode_epoch": row["mode_epoch"],
                                "version": row["version"],
                            }
                        )
                        state["expected_epoch"] = row["mode_epoch"]
                    for item in items:
                        # 每次发送前重新鉴权及核验有效分派，事务结束后才await网络。
                        latest = await run_in_threadpool(
                            ws_transaction,
                            socket,
                            lambda session, identity, ref=ref: (
                                HumanService().authorized_conversation(session, identity, ref)
                            ),
                        )
                        if latest["mode_epoch"] != item["mode_epoch"]:
                            continue
                        await socket.send_json({"type": "event", "event": item})
                        state["delivered_seq"] = item["seq"]
                    state["after_seq"] = cursor
                if not subscriptions:
                    await run_in_threadpool(ws_transaction, socket, lambda session, identity: None)
            if now_tick - last_ping >= 15:
                await socket.send_json({"type": "ping"})
                last_ping = now_tick
            if now_tick - last_input >= 45:
                await socket.close(code=4408)
                return
    except WebSocketDisconnect:
        return  # 网络断开不修改人工状态。
    except TimeoutError:
        await socket.close(code=4408)
    except AppError as exc:
        await socket.send_json(
            {
                "type": "access.revoked" if exc.status in {401, 403, 404} else "error",
                "code": exc.code,
                "message": exc.message,
            }
        )
        await socket.close(code=4403 if exc.status in {401, 403, 404} else 4409)
    except (ValueError, TypeError):
        await socket.send_json(
            {"type": "error", "code": "invalid_input", "message": "消息格式无效"}
        )
        await socket.close(code=4400)
