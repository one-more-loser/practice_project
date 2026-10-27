from hmac import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from cs_agent.core.errors import AppError, not_found
from cs_agent.core.responses import envelope
from cs_agent.db.session import session_dependency
from cs_agent.operations_snapshot.service import produce_snapshot

router = APIRouter(prefix="/api/v1/internal/stores")
DB = Annotated[Session, Depends(session_dependency, scope="function")]


def require_service(settings, authorization, store_id):
    token, stores = settings.activity_service_token, settings.activity_service_stores
    if not token or not stores:
        raise AppError("unavailable", 503, "经营源服务尚未配置")
    if not compare_digest(authorization.encode("utf-8"),
                          ("Bearer " + token.get_secret_value()).encode("utf-8")):
        raise AppError("unauthenticated", 401, "服务身份无效")
    if store_id not in stores:
        raise not_found()


@router.get("/{store_id}/operations-snapshot")
def operations_snapshot(store_id: str, request: Request, session: DB):
    settings = request.app.state.settings
    require_service(settings, request.headers.get("authorization", ""), store_id)
    payload = produce_snapshot(session, store_id, request.app.state.clock.now(),
                               inputs_file=settings.activity_snapshot_inputs_file)
    return envelope(request, payload)
