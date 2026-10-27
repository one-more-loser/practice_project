import asyncio
import logging
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from cs_agent.audit.service import record
from cs_agent.config import Settings
from cs_agent.core.clock import Clock
from cs_agent.core.errors import AppError
from cs_agent.core.responses import envelope
from cs_agent.db.schema import SCHEMA_REVISION
from cs_agent.db.session import Database

logger = logging.getLogger("cs_agent.requests")


def error_response(request, code, status, message):
    return JSONResponse(
        {
            "contract_version": "API-CS-v0.1",
            "request_id": request.state.request_id,
            "error": {
                "code": code,
                "message": message,
                "retryable": code in {"unavailable", "timeout"},
            },
        },
        status_code=status,
    )


def create_app(settings: Settings | None = None, clock: Clock | None = None):
    settings = settings or Settings()
    from cs_agent.core.request_metrics import install_metrics, request_metrics

    if settings.integration_run_id:
        install_metrics()

    @asynccontextmanager
    async def lifespan(app):
        db = None
        worker = None
        agent = None
        agent_task = None
        cancellation_worker = None
        cancellation_task = None
        activity_task = None
        app.state.agent_runtime = "disabled"
        stop = asyncio.Event()
        try:
            db = Database(settings)
            with db.engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                if revision != SCHEMA_REVISION:
                    raise RuntimeError("schema mismatch")
            app.state.db = db
            if settings.expiration_worker_enabled:

                async def expire_loop():
                    from cs_agent.commerce.expiration import expire_due

                    def sweep():
                        with db.transaction() as session:
                            expire_due(session, app.state.clock.now())

                    while not stop.is_set():
                        try:
                            await asyncio.to_thread(sweep)
                        except Exception:
                            logger.error("unpaid_expiration_unavailable")
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=5)
                        except TimeoutError:
                            pass

                worker = asyncio.create_task(expire_loop())
            if settings.activity_delivery_worker_enabled:

                async def activity_loop():
                    from cs_agent.activities.outbox import tick

                    while not stop.is_set():
                        try:
                            await asyncio.to_thread(tick, db, settings, app.state.clock)
                        except Exception:
                            logger.error("activity_delivery_unavailable")
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=1)
                        except TimeoutError:
                            pass

                activity_task = asyncio.create_task(activity_loop())
        except Exception:
            # 未配置、隔离不符或Schema落后均不冒充就绪，不输出含URL的异常。
            if db:
                db.close()
            app.state.db = None
        if app.state.db and settings.agent_worker_enabled:
            try:
                from cs_agent.runtime.worker import AgentWorker

                agent = AgentWorker(db, settings, app.state.clock, gateway=app.state.model_gateway)
                agent_task = asyncio.create_task(agent.run(stop))
                from cs_agent.cancellation import CancellationWorker

                cancellation_worker = CancellationWorker(
                    db, settings, app.state.clock, gateway=app.state.model_gateway
                )
                cancellation_task = asyncio.create_task(cancellation_worker.run(stop))
                app.state.agent_runtime = "ready"
            except Exception:
                app.state.agent_runtime = "unready"
                logger.error("agent_runtime_unavailable")
        yield
        stop.set()
        if worker:
            await worker
        if agent_task:
            await agent_task
        if cancellation_task:
            await cancellation_task
        if activity_task:
            await activity_task
        if cancellation_worker:
            cancellation_worker.close()
        if agent:
            agent.close()
        if db:
            db.close()

    app = FastAPI(title="店铺客服", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.clock, app.state.db = settings, clock or Clock(), None
    from cs_agent.models.gateway import ModelGateway

    app.state.model_gateway = ModelGateway.from_settings(settings)
    app.state.agent_runtime = "starting"

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        started = perf_counter()
        measurement = {"queries": 0, "sql_ms": 0.0}
        metrics_token = request_metrics.set(measurement) if settings.integration_run_id else None
        request.state.request_id = "req_" + uuid4().hex
        request.state.ctx = None
        request.state.audit_identity = None
        try:
            response = await call_next(request)
        except Exception:
            response = error_response(request, "internal_error", 500, "服务暂不可用")
        if response.status_code >= 400 and app.state.db:
            try:
                with app.state.db.transaction() as session:
                    record(
                        session,
                        request_id=request.state.request_id,
                        action="http.request.rejected",
                        outcome="denied",
                        ctx=request.state.ctx or request.state.audit_identity,
                        summary={"reason_code": "access_denied"},
                    )
            except Exception:
                # 已拒绝的请求不因审计存储故障变成成功；基础就绪检测会反映DB问题。
                logger.error("audit_unavailable request_id=%s", request.state.request_id)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Request-ID"] = request.state.request_id
        if metrics_token is not None:
            response.headers["X-Joint-Query-Count"] = str(measurement["queries"])
            response.headers["X-Joint-SQL-Ms"] = f"{measurement['sql_ms']:.3f}"
            response.headers["Server-Timing"] = f"db;dur={measurement['sql_ms']:.3f}, app;dur={(perf_counter()-started)*1000:.3f}"
            request_metrics.reset(metrics_token)
        logger.info(
            "request_id=%s method=%s status=%s",
            request.state.request_id,
            request.method,
            response.status_code,
        )
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request, exc):
        return error_response(request, exc.code, exc.status, exc.message)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return error_response(request, "unavailable", 503, "数据库暂不可用，请核对后重试")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # 不使用exc.errors()：默认内容包含非法请求的原始输入。
        return error_response(request, "invalid_input", 422, "请求字段不符合契约")

    from starlette.exceptions import HTTPException

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(
            request,
            "not_found" if exc.status_code == 404 else "invalid_input",
            exc.status_code,
            "资源不存在或不可访问" if exc.status_code == 404 else "请求不符合契约",
        )

    @app.get("/health/live")
    def live(request: Request):
        return envelope(request, {"process": "live"})

    @app.get("/health/ready")
    def ready(request: Request):
        available = False
        if app.state.db:
            try:
                with app.state.db.engine.connect() as conn:
                    available = (
                        conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                        == SCHEMA_REVISION
                    )
            except Exception:
                pass
        data = {
            "integration_run_id": settings.integration_run_id,
            "loaded_source_digest": settings.loaded_source_digest,
            "schema_revision": SCHEMA_REVISION,
            "scope": "V1_business_partial_acceptance",
            "database": "ready" if available else "unready",
            "chat_model": "unvalidated",
            "sql_model": "unvalidated",
            "embedding": "unvalidated",
            "reranker": "unvalidated",
            "agent_runtime": app.state.agent_runtime,
            "configured_capabilities": sorted(app.state.model_gateway.configs),
            "business_features": "commerce_reads_cancellation_support_returns_transport_experience_feedback",
        }
        return JSONResponse(envelope(request, data), status_code=200 if available else 503)

    @app.get("/api/v1/public/service-info")
    def service_info(request: Request):
        return envelope(
            request, {"store_name": "橙日生活", "service_name": "店铺客服", "stage": "V1_business"}
        )

    from cs_agent.identity.api import router

    app.include_router(router)
    from cs_agent.business_api import router as business_router

    app.include_router(business_router)
    from cs_agent.conversation.api import router as conversation_router

    app.include_router(conversation_router)
    from cs_agent.knowledge.api import router as knowledge_router

    app.include_router(knowledge_router)
    from cs_agent.aftersales.api import router as aftersales_router
    from cs_agent.cancellation.api import router as cancellation_router
    from cs_agent.support.api import router as support_router

    app.include_router(cancellation_router)
    app.include_router(support_router)
    app.include_router(aftersales_router)
    from cs_agent.experience.api import router as experience_router
    from cs_agent.feedback.api import router as feedback_router

    app.include_router(experience_router)
    app.include_router(feedback_router)
    from cs_agent.activities.api import router as activities_router
    from cs_agent.operations_snapshot.api import router as operations_snapshot_router

    app.include_router(activities_router)
    app.include_router(operations_snapshot_router)
    return app


app = create_app()
