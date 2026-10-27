"""同源管理API；不信任客户端店铺、角色或成本权限。"""

import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from ops_agent.auth import ROLES, Identity, Principal
from ops_agent.bridge import Bridge
from ops_agent.csv_import import parse_csv_bundle
from ops_agent.db import Database
from ops_agent.domain import DomainError
from ops_agent.errors import AppError
from ops_agent.query import Queries, query_ref
from ops_agent.query import schema as query_schema
from ops_agent.seed import demo_snapshot
from ops_agent.service import Operations

PREFIX = "/api/ops/v1"


def create_app(*, database_url, mode="integration", origin="http://127.0.0.1:5174",
               clock=None, identity_base_url=None, trade_base_url=None, service_token=None,
               service_stores=(), identity_transport=None, trade_transport=None):
    if mode not in ("integration", "demo", "test"):
        raise ValueError("mode只支持integration/demo/test")
    if mode == "integration" and (identity_transport is not None or trade_transport is not None):
        raise ValueError("真实接入禁止注入测试transport")
    clock = clock or (lambda: datetime.now(timezone.utc))
    database = Database(database_url, mode)
    identity = Identity(database, mode, identity_base_url, origin, clock, identity_transport)
    bridge = Bridge(database, clock, base_url=trade_base_url, service_token=service_token,
                    service_stores=service_stores, transport=trade_transport, origin=origin)
    operations = Operations(database, clock, mode, bridge)
    queries = Queries(database, clock)

    @asynccontextmanager
    async def lifespan(app):
        if mode in ("demo", "test"):
            database.initialize()
        else:
            database.verify_schema()
        if mode == "demo":
            p = Principal("demo_seed", "store_demo", ROLES["owner"], "", mode="demo")
            if not operations.list_snapshots(p)["items"]:
                operations.import_snapshot(p, demo_snapshot(clock()))
        yield
        bridge.close()
        identity.close()
        database.close()

    app = FastAPI(title="02店铺经营工作台", version="0.1.0", lifespan=lifespan,
                  docs_url=None if mode == "integration" else "/docs")
    app.state.database = database
    app.state.operations = operations
    app.state.bridge = bridge

    def failed(request, code, message, status):
        return JSONResponse({"error": {"code": code, "message": message},
                             "request_id": getattr(request.state, "request_id", uuid.uuid4().hex)},
                            status_code=status)

    @app.exception_handler(AppError)
    async def application_error(request, exc):
        return failed(request, exc.code, exc.message, exc.status)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return failed(request, exc.code, exc.message,
                      409 if exc.code == "snapshot_revision_conflict" else 422)

    @app.exception_handler(RequestValidationError)
    async def input_error(request, exc):
        return failed(request, "invalid_input", "请求字段、类型或查询参数不符", 422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        # 不把SQL、DSN、Cookie或快照正文写入响应/错误日志。
        return failed(request, "storage_unavailable", "经营存储暂不可用，请稍后按原幂等键核对", 503)

    @app.middleware("http")
    async def request_boundary(request, call_next):
        request.state.request_id = uuid.uuid4().hex
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if len(body) > 8 * 1024 * 1024:
                return failed(request, "payload_too_large", "单次请求上限8MiB", 413)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def principal(request, *caps, write=False):
        p = identity.resolve(request)
        p.require(*caps)
        if write:
            identity.check_write(request, p)
        return p

    def ok(data):
        return {"data": data}

    def context(p):
        return {"actor_ref": p.actor_ref, "store_id": p.store_id,
                "display_name": p.display_name, "capabilities": list(p.capabilities),
                "csrf_token": p.csrf_token, "mode": "demo" if mode == "demo" else "integration",
                "capability_revision": p.capability_revision, "auth_version": p.auth_version, "clock": clock().isoformat(),
                "dependencies": {"identity": "demo" if mode == "demo" else "connected",
                                 "activity": bridge.dependency_status, "model": "disabled"}}

    def key(request):
        return request.headers.get("Idempotency-Key")

    @app.get("/health")
    def health():
        return {"status": "alive", "service": "ops02", "version": "0.1.0"}

    @app.get(PREFIX + "/bootstrap")
    def bootstrap():
        return ok({"mode": "demo" if mode == "demo" else "integration",
                   "demo_available": mode == "demo", "login_url": "/login?next=%2Foperations%2F"})

    @app.get(PREFIX + "/context")
    def get_context(request: Request):
        return ok(context(principal(request)))

    @app.post(PREFIX + "/demo/session")
    def demo_session(request: Request, body: dict):
        # 先按mode拒绝，真实环境不暴露可选管理员身份。
        if mode != "demo":
            raise AppError("not_found", "资源不存在或不可访问", 404)
        identity.check_origin(request)
        if set(body) != {"role"}:
            raise AppError("invalid_input", "请选择演示角色", 422)
        token = identity.create_demo(body["role"])
        p = identity.resolve(SimpleNamespace(cookies={"ops_demo_session": token}))
        response = JSONResponse(ok(context(p)))
        response.set_cookie("ops_demo_session", token, httponly=True, samesite="strict",
                            secure=origin.startswith("https:"), max_age=28800, path="/")
        return response

    @app.get(PREFIX + "/snapshots")
    def snapshots(request: Request):
        return ok(operations.list_snapshots(principal(request)))

    @app.post(PREFIX + "/snapshots")
    def import_snapshot(request: Request, body: dict):
        value, created = operations.import_snapshot(principal(request, write=True), body)
        return JSONResponse(ok(value), status_code=201 if created else 200)

    @app.post(PREFIX + "/snapshots/import-csv")
    def import_csv(request: Request, body: dict):
        p = principal(request, "data.import", "cost.read", write=True)
        value, created = operations.import_snapshot(p, parse_csv_bundle(body))
        return JSONResponse(ok(value), status_code=201 if created else 200)

    @app.post(PREFIX + "/snapshots/pull")
    def pull_snapshot(request: Request):
        p = principal(request, "data.import", "cost.read", write=True)
        value, created = operations.import_snapshot(p, bridge.pull_snapshot(p.store_id))
        return JSONResponse(ok(value), status_code=201 if created else 200)

    @app.get(PREFIX + "/query/schema")
    def get_query_schema(request: Request):
        principal(request, "metrics.read")
        return ok(query_schema())

    @app.post(PREFIX + "/query")
    def controlled_query(request: Request, body: dict):
        began, ref, p = time.monotonic(), query_ref(), None
        try:
            p = identity.resolve(request)
            p.require("metrics.read")
            identity.check_write(request, p)
            result = queries.execute(p, body, request.state.request_id, ref)
        except SQLAlchemyError:
            queries.audit(request.state.request_id, ref, body, p, "storage_unavailable",
                          (time.monotonic() - began) * 1000)
            raise
        except (AppError, DomainError) as exc:
            queries.audit(request.state.request_id, ref, body, p, exc.code,
                          (time.monotonic() - began) * 1000)
            raise
        queries.audit(request.state.request_id, ref, body, p, "success",
                      (time.monotonic() - began) * 1000, result)
        return ok(result)

    @app.get(PREFIX + "/dashboard")
    def dashboard(request: Request, snapshot_id: str | None = None,
                  start_at: str | None = None, end_at: str | None = None,
                  sku_id: str | None = None, channel: str | None = None):
        return ok(operations.dashboard(principal(request), snapshot_id, start_at, end_at, sku_id, channel))

    @app.get(PREFIX + "/plans")
    def plans(request: Request):
        return ok(operations.list_plans(principal(request)))

    @app.post(PREFIX + "/plans", status_code=201)
    def create_plan(request: Request, body: dict):
        return ok(operations.save_plan(principal(request, "plan.edit", "cost.read", write=True),
                                       body, key(request)))

    @app.get(PREFIX + "/plans/{plan_id}")
    def get_plan(request: Request, plan_id: str, version: int | None = None):
        return ok(operations.get_plan(principal(request), plan_id, version))

    @app.put(PREFIX + "/plans/{plan_id}")
    def update_plan(request: Request, plan_id: str, body: dict):
        if set(body) != {"expected_version", "draft"}:
            raise AppError("invalid_input", "保存须含expected_version和draft", 422)
        return ok(operations.save_plan(principal(request, write=True), body["draft"], key(request),
                                       plan_id, body["expected_version"]))

    @app.post(PREFIX + "/plans/{plan_id}/evaluate")
    def evaluate(request: Request, plan_id: str, body: dict):
        return ok(operations.evaluate(principal(request, write=True), plan_id, body.get("expected_version")))

    @app.post(PREFIX + "/plans/{plan_id}/confirm")
    def confirm(request: Request, plan_id: str, body: dict):
        return ok(operations.confirm(principal(request, write=True), plan_id,
            body.get("expected_version"), body.get("reason"), key(request), request))

    @app.get(PREFIX + "/plans/{plan_id}/export")
    def export(request: Request, plan_id: str, format: str = "markdown"):
        text, media = operations.export(principal(request), plan_id, format)
        return Response(text, media_type=media, headers={"Content-Disposition":
            f'attachment; filename="operations-plan.{"csv" if format == "csv" else "md"}"'})

    @app.post(PREFIX + "/plans/{plan_id}/cancel-approval")
    def cancel_approval(request: Request, plan_id: str, body: dict):
        return ok(operations.cancel_approval(principal(request, write=True), plan_id,
            body.get("expected_version"), body.get("reason"), key(request)))

    @app.post(PREFIX + "/plans/{plan_id}/reconcile-execution")
    def reconcile_execution(request: Request, plan_id: str, body: dict):
        return ok(operations.reconcile_execution(principal(request, write=True), plan_id,
                                                body.get("expected_version")))

    @app.post(PREFIX + "/plans/{plan_id}/execution-preview")
    def preview(request: Request, plan_id: str):
        return ok(operations.preview(principal(request, write=True), plan_id, request))

    @app.post(PREFIX + "/assistant")
    def assistant(request: Request, body: dict):
        return ok(operations.assistant(principal(request, write=True), body))

    @app.get(PREFIX + "/activities")
    def activities(request: Request):
        return ok(operations.activity_list(principal(request)))

    @app.get(PREFIX + "/monitoring")
    def monitoring(request: Request, plan_id: str):
        return ok(operations.monitor(principal(request), plan_id))

    @app.post(PREFIX + "/retrospectives")
    def retrospective(request: Request, body: dict):
        return ok(operations.retrospective(principal(request, write=True), body, key(request)))

    def service_auth(request, store_id):
        bridge.authenticate(request.headers, store_id)

    @app.get(PREFIX + "/internal/plan-revisions/{plan_id}")
    def projection(request: Request, plan_id: str, store_id: str):
        service_auth(request, store_id)
        return ok(bridge.projection(store_id, plan_id))

    @app.post(PREFIX + "/internal/execution-grants")
    def grant(request: Request, body: dict):
        store_id = body.get("store_id")
        service_auth(request, store_id)
        payload = {k: v for k, v in body.items() if k not in ("store_id", "plan_id")}
        return ok(bridge.grant(store_id, body.get("plan_id"), payload))

    @app.post(PREFIX + "/internal/execution-receipts")
    def receipt(request: Request, body: dict):
        store_id = body.get("store_id")
        service_auth(request, store_id)
        return ok(bridge.receipt(store_id, body))

    @app.post(PREFIX + "/internal/activity-actuals")
    def actuals(request: Request, body: dict):
        store_id = body.get("store_id")
        service_auth(request, store_id)
        return ok(bridge.actuals(store_id, body))

    @app.post(PREFIX + "/internal/executions/{intent_id}/reconcile")
    def reconcile(request: Request, intent_id: str, body: dict):
        store_id = body.get("store_id")
        service_auth(request, store_id)
        return ok(bridge.reconcile(store_id, intent_id, finalize=body.get("finalize") is True))

    return app
