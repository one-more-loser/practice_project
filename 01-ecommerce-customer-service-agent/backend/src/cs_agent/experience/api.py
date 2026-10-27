from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field

from cs_agent.business_api import DB, context
from cs_agent.core.errors import AppError
from cs_agent.core.types import ContractModel
from cs_agent.experience.policy import Procedure
from cs_agent.experience.service import ExperienceService
from cs_agent.main import envelope

router = APIRouter(prefix="/api/v1/staff/experience")
service = ExperienceService()


class Candidate(ContractModel):
    kind: Literal["case", "skill"]
    name: str = Field(min_length=1, max_length=160)
    topic: str = Field(min_length=1, max_length=40)
    definition: Procedure
    source_ticket_id: str | None = Field(default=None, max_length=80)
    source_case_ids: list[str] = Field(default_factory=list, max_length=20)
    previous_version_id: str | None = Field(default=None, max_length=80)
    valid_until: datetime | None = None


class Generate(ContractModel):
    source_case_ids: list[str] = Field(min_length=1, max_length=20)


class GenerateCase(ContractModel):
    source_ticket_id: str = Field(min_length=1, max_length=80)


@router.get("")
def listing(request: Request, session: DB):
    return envelope(request, service.list(session, context(session, request)))


@router.post("")
def create(body: Candidate, request: Request, session: DB):
    return envelope(
        request,
        service.create(
            session,
            context(session, request, True),
            body.kind,
            body.model_dump(exclude={"kind"}),
            request.app.state.clock.now(),
        ),
    )


@router.post("/generate-skill")
def generate(body: Generate, request: Request):
    if request.app.state.db is None:
        raise AppError("unavailable", 503, "数据库尚未就绪")
    return envelope(
        request,
        service.model_candidate(
            request.app.state.db, request, body.model_dump(), request.app.state.model_gateway
        ),
    )


@router.post("/generate-case")
def generate_case(body: GenerateCase, request: Request):
    if request.app.state.db is None:
        raise AppError("unavailable", 503, "数据库尚未就绪")
    return envelope(
        request,
        service.model_case(
            request.app.state.db, request, body.source_ticket_id, request.app.state.model_gateway
        ),
    )


@router.post("/{ref}/{action}")
def govern(
    ref: str,
    action: Literal["review", "regress", "publish", "retire", "rollback"],
    request: Request,
    session: DB,
):
    ctx = context(session, request, True)
    now = request.app.state.clock.now()
    if action in {"publish", "retire"}:
        result = service.publish(session, ctx, ref, now, retire=action == "retire")
    elif action == "rollback":
        result = service.rollback(session, ctx, ref, now)
    elif action == "review":
        result = service.review(session, ctx, ref, now)
    else:
        result = service.regress(session, ctx, ref, now)
    return envelope(request, result)
