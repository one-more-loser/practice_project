"""正式售后与运输异常共享确认、幂等和后端角色边界。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request
from pydantic import Field

from cs_agent.aftersales.inspection import InspectionService
from cs_agent.aftersales.service import AftersaleService
from cs_agent.business_api import DB, context, operation_response
from cs_agent.core.types import ContractModel
from cs_agent.main import envelope
from cs_agent.transport.service import TransportService

router = APIRouter(prefix="/api/v1")
KEY = Annotated[str, Header(alias="Idempotency-Key")]
LIMIT = Annotated[int, Query(ge=1, le=50)]
OFFSET = Annotated[int, Query(ge=0)]


class Version(ContractModel):
    expected_version: int = Field(ge=1, strict=True)


class Review(Version):
    decision: Literal[
        "approve",
        "reject",
        "expedited",
        "intercept_requested",
        "intercept_failed",
        "loss_pending",
        "loss_verified",
        "dispute",
        "close",
    ]
    basis: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(min_length=1, max_length=12)


class ReturnShipment(Version):
    carrier: str = Field(min_length=1, max_length=60)
    tracking_no: str = Field(min_length=6, max_length=80)


class Inspection(Version):
    lines: dict
    evidence_refs: list[str] = Field(min_length=1, max_length=12)


@router.get("/aftersales")
def listing(request: Request, session: DB, limit: LIMIT = 20, offset: OFFSET = 0):
    return envelope(
        request,
        {
            "items": AftersaleService().list(
                session, context(session, request), limit=limit, offset=offset
            )
        },
    )


@router.get("/aftersales/{ref}")
def detail(ref: str, request: Request, session: DB):
    return envelope(request, AftersaleService().read(session, context(session, request), ref))


@router.post("/aftersales/{ref}/withdraw")
def withdraw(ref: str, body: Version, request: Request, session: DB, key: KEY):
    return operation_response(
        request,
        *AftersaleService().withdraw(
            session,
            context(session, request, True),
            ref,
            body.expected_version,
            key,
            request.app.state.clock.now(),
        ),
    )


@router.post("/aftersales/{ref}/reviews")
def review(ref: str, body: Review, request: Request, session: DB, key: KEY):
    return operation_response(
        request,
        *AftersaleService().review(
            session,
            context(session, request, True),
            ref,
            body.decision,
            body.basis,
            body.evidence_refs,
            body.expected_version,
            key,
            request.app.state.clock.now(),
        ),
    )


@router.post("/aftersales/{ref}/return-shipment")
def return_shipment(ref: str, body: ReturnShipment, request: Request, session: DB, key: KEY):
    return operation_response(
        request,
        *AftersaleService().return_shipment(
            session,
            context(session, request, True),
            ref,
            body.carrier,
            body.tracking_no,
            body.expected_version,
            key,
            request.app.state.clock.now(),
        ),
    )


@router.post("/refund-claims/{ref}/inspections")
def inspect(ref: str, body: Inspection, request: Request, session: DB, key: KEY):
    return operation_response(
        request,
        *InspectionService().inspect(
            session,
            context(session, request, True),
            ref,
            body.lines,
            body.evidence_refs,
            body.expected_version,
            key,
            request.app.state.clock.now(),
        ),
    )


@router.get("/transport-incidents")
def transport_listing(request: Request, session: DB, limit: LIMIT = 20, offset: OFFSET = 0):
    return envelope(
        request,
        {
            "items": TransportService().list(
                session, context(session, request), limit=limit, offset=offset
            )
        },
    )


@router.get("/transport-incidents/{ref}")
def transport_detail(ref: str, request: Request, session: DB):
    return envelope(request, TransportService().read(session, context(session, request), ref))


@router.post("/transport-incidents/{ref}/reviews")
def transport_review(ref: str, body: Review, request: Request, session: DB, key: KEY):
    return operation_response(
        request,
        *TransportService().review(
            session,
            context(session, request, True),
            ref,
            body.decision,
            body.basis,
            body.evidence_refs,
            body.expected_version,
            key,
            request.app.state.clock.now(),
        ),
    )
