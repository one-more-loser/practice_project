from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql

from cs_agent.activities.client import OperationsClient
from cs_agent.activities.outbox import claim
from cs_agent.activities.repository import retry_delay
from cs_agent.core.errors import AppError


def settings():
    return SimpleNamespace(
        activity_ops_base_url="http://ops02.test",
        activity_service_token=SecretStr("test-only-token"),
        activity_service_stores=("store_sim",),
    )


@pytest.mark.parametrize("data", [None, [], 42, "wrong"])
def test_bad_remote_data_stays_unknown(data):
    client = OperationsClient(
        settings(),
        "store_sim",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data})),
    )
    with pytest.raises(AppError) as error:
        client.request("GET", "/revision")
    assert error.value.status == 503


def test_remote_no_redirect_and_bearer_bound():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(302, headers={"Location": "http://untrusted.test"})

    client = OperationsClient(settings(), "store_sim", transport=httpx.MockTransport(handler))
    with pytest.raises(AppError):
        client.request("GET", "/revision")
    assert len(calls) == 1
    assert calls[0].headers["authorization"] == "Bearer test-only-token"


def test_unconfigured_and_other_store_rejected():
    for value, store in [(settings(), "other"), (SimpleNamespace(), "store_sim")]:
        with pytest.raises(AppError):
            OperationsClient(value, store)


def test_claim_has_durable_order_lease_and_skip_locked():
    class Rows:
        def mappings(self):
            return self

        def first(self):
            return None

    class Session:
        def execute(self, stmt):
            sql = str(stmt.compile(dialect=postgresql.dialect()))
            assert "SKIP LOCKED" in sql and "earlier.sequence < activity_delivery.sequence" in sql
            assert "NOT (EXISTS" in sql and "lease_until" in sql
            return Rows()

    assert claim(Session(), datetime.now(UTC), ("store_sim",)) is None
    assert retry_delay(99).total_seconds() == 256
