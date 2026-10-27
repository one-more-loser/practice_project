from typing import Annotated

from fastapi import APIRouter, Query, Request

from cs_agent.cancellation.service import CancellationService
from cs_agent.identity.api import DB
from cs_agent.identity.service import authenticate
from cs_agent.main import envelope

router = APIRouter(prefix="/api/v1/me/cancellation-reviews")
service = CancellationService()


@router.get("")
def list_reviews(
    request: Request,
    session: DB,
    order_ref: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
):
    ctx, *_ = authenticate(session, request)
    return envelope(request, {"items": service.list(session, ctx, order_ref, limit, offset)})


@router.get("/{review_id}")
def get_review(review_id: str, request: Request, session: DB):
    ctx, *_ = authenticate(session, request)
    return envelope(request, service.get(session, ctx, review_id))
