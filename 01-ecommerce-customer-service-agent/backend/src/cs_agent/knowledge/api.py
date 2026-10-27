from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import Field

from cs_agent.conversation.identity import principal_for
from cs_agent.core.types import ContractModel
from cs_agent.identity.api import DB
from cs_agent.identity.service import authenticate, require_csrf
from cs_agent.knowledge.service import KnowledgeService
from cs_agent.main import envelope

router = APIRouter(prefix="/api/v1/knowledge")
service = KnowledgeService()


class Empty(ContractModel):
    pass


class DraftInput(ContractModel):
    title: str = Field(min_length=1, max_length=160)
    topic: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=12000)
    visibility: str = Field(pattern="^(public|customer)$")
    source_ref: str = Field(min_length=1, max_length=160)
    source_revision: str = Field(min_length=1, max_length=80)
    valid_from: datetime
    valid_until: datetime | None = None
    product_id: str | None = Field(default=None, min_length=1, max_length=80)


class RevisionInput(ContractModel):
    body: str = Field(min_length=1, max_length=12000)
    source_ref: str = Field(min_length=1, max_length=160)
    source_revision: str = Field(min_length=1, max_length=80)
    valid_from: datetime
    valid_until: datetime | None = None


class FaqInput(ContractModel):
    question: str = Field(min_length=1, max_length=600)
    variants: list[str] = Field(default_factory=list, max_length=8)
    answer: str = Field(min_length=1, max_length=4000)
    source_ref: str = Field(min_length=1, max_length=160)
    source_revision: str = Field(min_length=1, max_length=80)
    topic: str = Field(min_length=1, max_length=80)
    visibility: str = Field(pattern="^(public|customer)$")
    product_id: str | None = Field(default=None, min_length=1, max_length=80)
    knowledge_revision_id: str | None = Field(default=None, min_length=1, max_length=80)
    valid_from: datetime
    valid_until: datetime | None = None


@router.get("")
def list_content(request: Request, session: DB):
    ctx, _, _, _ = authenticate(session, request)
    return envelope(request, service.list_admin(session, ctx))


@router.post("/units", status_code=201)
def create_unit(body: DraftInput, request: Request, session: DB):
    ctx, current, _, _ = authenticate(session, request)
    require_csrf(request, current)
    result = service.create_draft(
        session, ctx, **body.model_dump(), now=request.app.state.clock.now()
    )
    return envelope(request, result)


@router.post("/units/{unit_id}/revisions", status_code=201)
def new_revision(unit_id: str, body: RevisionInput, request: Request, session: DB):
    ctx, current, _, _ = authenticate(session, request)
    require_csrf(request, current)
    return envelope(
        request,
        service.create_revision(
            session, ctx, unit_id, **body.model_dump(), now=request.app.state.clock.now()
        ),
    )


@router.post("/units/{unit_id}/retire")
def retire_unit(unit_id: str, body: Empty, request: Request, session: DB):
    ctx, current, _, _ = authenticate(session, request)
    require_csrf(request, current)
    return envelope(
        request, service.retire_unit(session, ctx, unit_id, request.app.state.clock.now())
    )


@router.post("/faqs", status_code=201)
def new_faq(body: FaqInput, request: Request, session: DB):
    ctx, current, _, _ = authenticate(session, request)
    require_csrf(request, current)
    return envelope(
        request, service.create_faq(session, ctx, body.model_dump(), request.app.state.clock.now())
    )


@router.post("/faqs/{faq_id}/revisions", status_code=201)
def new_faq_revision(faq_id: str, body: FaqInput, request: Request, session: DB):
    ctx, current, _, _ = authenticate(session, request)
    require_csrf(request, current)
    return envelope(
        request,
        service.create_faq(session, ctx, body.model_dump(), request.app.state.clock.now(), faq_id),
    )


@router.get("/admin/{kind}/{ref}")
def inspect_admin(kind: str, ref: str, request: Request, session: DB):
    ctx, _, _, _ = authenticate(session, request)
    return envelope(request, service.inspect_admin(session, ctx, kind, ref))


@router.post("/revisions/{revision_id}/review")
def review_revision(revision_id: str, body: Empty, request: Request, session: DB):
    ctx, current, _, _ = authenticate(session, request)
    require_csrf(request, current)
    return envelope(
        request, service.review_draft(session, ctx, revision_id, request.app.state.clock.now())
    )


@router.post("/revisions/{revision_id}/publish")
def publish_revision(revision_id: str, body: Empty, request: Request):
    db = request.app.state.db
    if db is None:
        from cs_agent.core.errors import AppError

        raise AppError("unavailable", 503, "数据库暂不可用")
    gateway = request.app.state.model_gateway
    result = service.publish_revision(
        db, request, revision_id, gateway, request.app.state.clock.now()
    )
    return envelope(request, result)


@router.post("/faqs/{faq_id}/review")
def review_faq(faq_id: str, body: Empty, request: Request, session: DB):
    ctx, current, _, _ = authenticate(session, request)
    require_csrf(request, current)
    return envelope(
        request, service.review_faq(session, ctx, faq_id, request.app.state.clock.now())
    )


@router.post("/faqs/{faq_id}/publish")
def publish_faq(faq_id: str, body: Empty, request: Request, session: DB):
    ctx, current, _, _ = authenticate(session, request)
    require_csrf(request, current)
    return envelope(
        request, service.publish_faq(session, ctx, faq_id, request.app.state.clock.now())
    )


@router.post("/faqs/{faq_id}/retire")
def retire_faq(faq_id: str, body: Empty, request: Request, session: DB):
    ctx, current, _, _ = authenticate(session, request)
    require_csrf(request, current)
    return envelope(
        request, service.retire_faq(session, ctx, faq_id, request.app.state.clock.now())
    )


@router.get("/references/{kind}/{reference_id}")
def reference(kind: str, reference_id: str, request: Request, session: DB):
    principal = principal_for(session, request)
    if kind == "knowledge":
        result = service.reference(session, principal, reference_id, request.app.state.clock.now())
    elif kind == "faq":
        result = service.faq_reference(
            session, principal, reference_id, request.app.state.clock.now()
        )
    else:
        from cs_agent.core.errors import AppError

        raise AppError("not_found", 404, "资源不存在或不可访问")
    return envelope(request, result)
