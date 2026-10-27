"""真实路由/工具/定价代码；SQL结果、身份、幂等持久层为内存适配器，无PG/模型。"""
import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

import cs_agent.main as app_module
from cs_agent import business_api
from cs_agent.agent.graph import AgentGraph
from cs_agent.catalog import service as catalog
from cs_agent.commerce import checkout
from cs_agent.config import Settings
from cs_agent.core.context import Role, TrustedContext
from cs_agent.db.session import session_dependency

WALL = datetime(2026, 10, 24, tzinfo=UTC)
START = WALL + timedelta(hours=1)
END = START + timedelta(hours=1)
CASES = [
    (START - timedelta(microseconds=1), "active", "10000"),
    (START, "scheduled", "9200"),
    (START + timedelta(minutes=10), "scheduled", "9200"),
    (END - timedelta(microseconds=1), "active", "9200"),
    (END, "active", "10000"),
    (END + timedelta(hours=1), "active", "10000"),
    (WALL - timedelta(hours=1), "active", "10000"),
    (START + timedelta(minutes=10), "paused", "10000"),
]


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return next(iter(self.rows), None)

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def __iter__(self):
        return iter(self.rows)


def context(role=Role.CUSTOMER):
    return TrustedContext("customer_a", "store_sim", role, "test_session", 1, "clock_batch", 1, "clock_request")


class MemorySQL:
    def __init__(self, phase):
        self.phase, self.info, self.seen_times = phase, {}, []

    def scalar(self, statement):
        return SimpleNamespace(purpose="checkout")

    def execute(self, statement):
        sql = str(statement)
        if "activity_execution" in sql:
            times = [v for v in statement.compile().params.values() if isinstance(v, datetime)]
            assert times and len(set(times)) == 1
            at = times[0]
            self.seen_times.append(at)
            return Result([dict(line_base=10000, line_price_version=1, effective_price_minor=9200,
                                activity_id="clock_activity", revision=1, phase=self.phase)]
                          if START <= at < END and self.phase in {"scheduled", "active"} else [])
        if re.search(r"\bFROM\s+cart_item\b", sql):
            return Result([dict(item_id="cart_item", sku_id="clock_sku", qty=1, selected=True, version=1)])
        if re.search(r"\bFROM\s+cart\b", sql):
            return Result([dict(cart_id="clock_cart", version=1)])
        if "JOIN inventory" in sql:
            return Result([dict(sku_id="clock_sku", product_id="clock_product", data_batch_id="clock_batch",
                                batch_generation=1, specifications={}, stock_qty=100, reserved_qty=0,
                                price_minor=10000, price_version=1, product_version=1, status="active",
                                product_status="active", name="Clock product", image_slug="cup",
                                version=1, inventory_version=1)])
        if "INSERT INTO checkout_quote" in sql:
            return Result([])
        return Result([dict(product_id="clock_product", name="Clock product", category="test", description="",
                            aliases=[], image_slug="cup", status="active", version=1,
                            source_kind="synthetic", updated_at=WALL)])


@pytest.fixture
def api_client(monkeypatch):
    sessions = []

    def make(at, phase, wall=WALL):
        class WallDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return wall

        monkeypatch.setattr(catalog, "datetime", WallDatetime)
        session = MemorySQL(phase)
        sessions.append(session)
        app = app_module.create_app(Settings(database_url=None, agent_worker_enabled=False, expiration_worker_enabled=False),
                         clock=SimpleNamespace(now=lambda: at))
        app.dependency_overrides[session_dependency] = lambda: session
        monkeypatch.setattr(business_api, "context", lambda session, request, write=False:
                            context(Role.SHOP_ADMIN if "/shop/" in request.url.path else Role.CUSTOMER))
        monkeypatch.setattr(checkout, "execute_once", lambda session, ctx, action, key, payload, now, execute, **kwargs:
                            (execute("memory_operation"), 200))
        client = TestClient(app)
        return client, session

    return make


@pytest.mark.parametrize("at,phase,expected", CASES)
def test_shop_catalog_route_uses_trusted_time(api_client, at, phase, expected):
    client, session = api_client(at, phase)
    response = client.get("/api/v1/shop/products")
    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["skus"][0]["price_minor"] == expected
    assert session.seen_times == [at]


@pytest.mark.parametrize("at,phase,expected", CASES)
def test_real_catalog_tool_branch_uses_trusted_clock(api_client, at, phase, expected):
    _, session = api_client(at, phase)
    assert tool_price(session, at) == expected
    assert session.seen_times == [at]


def tool_price(session, at):
    graph = AgentGraph.__new__(AgentGraph)
    graph.clock = SimpleNamespace(now=lambda: at)

    @contextmanager
    def guard():
        yield session, (None, None, SimpleNamespace(store_id="store_sim", context=context()))

    graph._guard = guard
    # 测试模型协议适配器产生真实catalog.read动作；不调用gateway或付费模型。
    action = graph._parse(AIMessage(content=json.dumps({"kind": "tool_call", "tool": "catalog.read",
                                                       "arguments": {"product_id": "clock_product"}})))
    result = graph._execute({"actions": action["calls"]})
    return result["tool_results"][0]["result"]["items"][0]["skus"][0]["price_minor"]


@pytest.mark.parametrize("at,phase,expected", CASES)
def test_public_cart_and_quote_keep_same_time_rule(api_client, at, phase, expected):
    client, session = api_client(at, phase)
    public = client.get("/api/v1/products").json()["data"]["items"][0]["skus"][0]
    cart = client.get("/api/v1/me/cart").json()["data"]["items"][0]
    quote = client.post("/api/v1/me/quotes", headers={"Idempotency-Key": "clock_quote"},
                        json={"items": [{"sku_id": "clock_sku", "qty": 1}],
                              "cart_item_ids": [], "shipping_input_ref": "shipping_test"})
    assert quote.status_code == 200
    assert public["price_minor"] == cart["price_minor"] == quote.json()["data"]["payable_minor"] == expected
    assert session.seen_times == [at] * 3


@pytest.mark.parametrize("wall,at,expected", [(END + timedelta(days=1), START, "9200"),
                                            (START, END + timedelta(days=1), "10000")])
def test_shop_and_tool_do_not_follow_ahead_or_behind_wall_clock(api_client, wall, at, expected):
    client, session = api_client(at, "active", wall)
    assert client.get("/api/v1/shop/products").json()["data"]["items"][0]["skus"][0]["price_minor"] == expected
    assert tool_price(session, at) == expected
    assert session.seen_times == [at, at]
