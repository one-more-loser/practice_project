import json
import os
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.core.errors import not_found
from cs_agent.db.models import ShippingInput
from cs_agent.db.seed import seed
from cs_agent.db.session import verify_target
from cs_agent.identity.policies import ResourceScope, require_fulfillment_contact, require_support
from cs_agent.identity.service import authenticate
from cs_agent.main import create_app, envelope
from cs_agent.privacy.projections import PrivateContact, support_contact


class TestClock:
    def __init__(self):
        self.value = datetime(2026, 10, 22, 3, tzinfo=UTC)

    def now(self):
        return self.value


class PrivateValues(dict):
    def __repr__(self):
        return "<redacted test values>"


@pytest.fixture
def environment():
    url = os.environ["CS_TEST_MIGRATION_URL"]
    engine = create_engine(url, hide_parameters=True)
    verify_target(engine, Settings(expected_database="cs_agent_b0_test"), migration=True)
    config = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    from sqlalchemy.orm import Session

    from cs_agent.db.seed_business import seed_accounts, seed_catalog
    from tests.helpers import reset_test_data

    reset_test_data(engine)
    seed(url)
    with Session(engine) as seed_session, seed_session.begin():
        seed_accounts(seed_session)
        seed_catalog(seed_session)
    clock = TestClock()
    settings = Settings(
        database_url=os.environ["CS_TEST_DATABASE_URL"],
        query_database_url=os.environ.get("CS_TEST_QUERY_DATABASE_URL"),
        expected_database="cs_agent_b0_test",
        cookie_secure=False,
        expiration_worker_enabled=False,
        agent_worker_enabled=False,
    )
    app = create_app(settings, clock)
    assignment = {"active": True, "staff_id": "support_a", "fulfillment_required": True}

    # 测试专用HTTP入口：模拟T-CS-015分派仓储，身份/数据库/投影和策略都使用真实实现。
    # 不注册在生产create_app，不能当作工单/导出API验收。
    def scoped_projection(ref: str, request: Request, fulfillment=False):
        with app.state.db.transaction() as session:
            ctx, _, _, _ = authenticate(session, request)
            row = session.get(ShippingInput, ref)
            if row is None:
                raise not_found()
            policy = require_fulfillment_contact if fulfillment else require_support
            policy(
                ctx,
                ResourceScope(
                    row.store_id,
                    row.owner_id,
                    assignment["staff_id"],
                    assignment["active"],
                    assignment["fulfillment_required"],
                ),
            )
            contact = PrivateContact(
                row.input_id,
                row.recipient,
                row.phone,
                row.province,
                row.city,
                row.district,
                row.street,
            )
            return envelope(request, support_contact(contact))

    @app.get("/_test/support/{ref}")
    def support_projection(ref: str, request: Request):
        return scoped_projection(ref, request)

    @app.get("/_test/fulfillment/{ref}")
    def fulfillment_projection(ref: str, request: Request):
        return scoped_projection(ref, request, fulfillment=True)

    with TestClient(app) as client:
        assert app.state.db is not None, "isolated database must be available"
        accounts = PrivateValues(
            json.loads((PROJECT_ROOT / ".runtime/demo-accounts.json").read_text())
        )
        yield client, engine, clock, assignment, accounts
    engine.dispose()


@pytest.fixture
def login_as(environment):
    client, _, _, _, accounts = environment

    def login(name):
        response = client.post(
            "/api/v1/session",
            json={"login_name": name, "password": accounts[name]},
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        assert response.status_code == 200, response.text
        return PrivateValues(
            {
                "Origin": "http://127.0.0.1:5173",
                "X-CSRF-Token": response.json()["data"]["csrf_token"],
            }
        )

    return login
