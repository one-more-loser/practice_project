import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from ops_agent import db
from ops_agent.errors import AppError

OWNER = ("metrics.read", "cost.read", "data.import", "plan.edit", "plan.confirm",
         "activity.publish", "activity.manage")
ROLES = {"owner": OWNER,
         "operator": ("metrics.read", "cost.read", "data.import", "plan.edit"),
         "viewer": ("metrics.read",)}


@dataclass(frozen=True)
class Principal:
    actor_ref: str
    store_id: str
    capabilities: tuple[str, ...]
    csrf_token: str
    display_name: str = "管理人员"
    auth_version: int = 1
    capability_revision: int = 1
    mode: str = "integration"

    def require(self, *capabilities):
        if not set(capabilities) <= set(self.capabilities):
            raise AppError("forbidden", "当前身份没有此操作或成本数据权限", 403)


def digest_token(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Identity:
    def __init__(self, database, mode, base_url, origin, clock, transport=None):
        self.database, self.mode, self.origin, self.clock = database, mode, origin, clock
        self.client = httpx.Client(base_url=base_url or "http://127.0.0.1", timeout=3,
                                   follow_redirects=False, transport=transport)
        self.configured = bool(base_url)

    def create_demo(self, role):
        if self.mode != "demo":
            raise AppError("not_found", "资源不存在或不可访问", 404)
        if not isinstance(role, str) or role not in ROLES:
            raise AppError("invalid_input", "请选择合法演示角色", 422)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        body = {"actor_ref": "demo_" + role, "store_id": "store_demo",
                "capabilities": list(ROLES[role]), "csrf_token": csrf,
                "display_name": {"owner": "演示店长", "operator": "演示运营", "viewer": "演示查看者"}[role],
                "expires_at": (self.clock() + timedelta(hours=8)).isoformat()}
        with self.database.transaction() as c:
            c.execute(db.sessions.insert().values(token_digest=digest_token(token), body=body))
        return token

    def resolve(self, request):
        if self.mode == "demo":
            token = request.cookies.get("ops_demo_session", "")
            with self.database.transaction() as c:
                body = c.execute(select(db.sessions.c.body).where(
                    db.sessions.c.token_digest == digest_token(token))).scalar_one_or_none()
            if not body or datetime.fromisoformat(body["expires_at"]) <= self.clock():
                raise AppError("unauthenticated", "请先进入演示店铺", 401)
            return Principal(body["actor_ref"], body["store_id"], tuple(body["capabilities"]),
                             body["csrf_token"], body["display_name"], mode="demo")
        token = request.cookies.get("cs_session")
        if not token:
            raise AppError("unauthenticated", "请先在商城登录管理账户", 401)
        if not self.configured:
            raise AppError("identity_unavailable", "01身份接口尚未配置", 503)
        try:
            response = self.client.get("/api/v1/session", headers={"Cookie": "cs_session=" + token})
            if response.status_code == 401:
                raise AppError("unauthenticated", "商城登录已失效，请重新登录", 401)
            response.raise_for_status()
            value = response.json()["data"]
            required = ("subject_id", "store_id", "capabilities", "csrf_token", "auth_version",
                        "capability_revision", "expires_at")
            if not all(key in value for key in required) or not isinstance(value["capabilities"], list):
                raise ValueError("identity contract")
            if (any(not isinstance(cap, str) for cap in value["capabilities"]) or
                    any(not isinstance(value[field], str) or not value[field]
                        for field in ("subject_id", "store_id", "csrf_token")) or
                    any(type(value[field]) is not int or value[field] < 1
                        for field in ("auth_version", "capability_revision"))):
                raise ValueError("identity field types")
            if datetime.fromisoformat(value["expires_at"]) <= self.clock():
                raise AppError("unauthenticated", "商城登录已过期", 401)
            return Principal(value["subject_id"], value["store_id"], tuple(value["capabilities"]),
                             value["csrf_token"], "店铺管理人员", value["auth_version"],
                             value["capability_revision"])
        except AppError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise AppError("identity_contract_unavailable", "01身份或能力契约未就绪，已停止访问", 503) from exc

    def check_origin(self, request):
        if request.headers.get("Origin") != self.origin:
            raise AppError("forbidden", "请求来源无效", 403)

    def check_write(self, request, principal):
        self.check_origin(request)
        if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), principal.csrf_token):
            raise AppError("forbidden", "页面校验已失效，请刷新", 403)

    def close(self):
        self.client.close()
