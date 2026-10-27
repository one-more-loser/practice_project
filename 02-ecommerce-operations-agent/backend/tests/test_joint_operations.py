"""本轮新增消费者/版本/来源/过滤回归，HTTP为显式替身。"""
from copy import deepcopy
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from test_domain import NOW, draft, snapshot

from ops_agent import db
from ops_agent.auth import OWNER, Principal
from ops_agent.bridge import Bridge
from ops_agent.domain import DomainError, validate_snapshot
from ops_agent.errors import AppError
from ops_agent.service import Operations, digest


@pytest.fixture
def setup(tmp_path):
    database = db.Database("sqlite:///" + str(tmp_path / "ops.db"), "test")
    database.initialize()
    bridge = Bridge(database, lambda: NOW)
    operations = Operations(database, lambda: NOW, "demo", bridge)
    principal = Principal("manager", "store_A", OWNER, "csrf", mode="demo")
    source = snapshot()
    operations.import_snapshot(principal, source)
    yield database, operations, principal, source
    bridge.close()
    database.close()


def test_upgrade_replays_and_preserves_existing_data(setup):
    database, operations, principal, source = setup
    database.initialize()
    database.initialize()
    assert operations.list_snapshots(principal)["items"] == [source]
    with database.transaction() as connection:
        assert list(connection.execute(select(db.schema_versions.c.version)).scalars()) == [1, 2]


def test_derived_requires_archived_bound_parent_and_exports_full_replay(setup):
    _, operations, principal, source = setup
    child = deepcopy(source)
    child.update(snapshot_id="derived_1", source_kind="derived", schema_version="OPS-SNAPSHOT-v0.2",
        provenance={"parents": [{"snapshot_id": source["snapshot_id"], "revision": 1, "digest": digest(source)}],
                    "transformation": "identity copy", "inputs_version": "gold-v1"})
    operations.import_snapshot(principal, child)
    spec = draft()
    spec["snapshot_id"] = child["snapshot_id"]
    plan = operations.save_plan(principal, spec, "new")
    for format in ("markdown", "csv"):
        report, _ = operations.export(principal, plan["plan_id"], format)
        assert "parent_archive" in report
        assert "OPS-SNAPSHOT-v0.2" in report
        assert "cost_minor" in report
    child["snapshot_id"] = "derived_bad"
    child["provenance"]["parents"][0]["digest"] = "0" * 64
    with pytest.raises(AppError, match="父快照"):
        operations.import_snapshot(principal, child)
    child.pop("provenance")
    with pytest.raises(DomainError):
        validate_snapshot(child)


@pytest.mark.parametrize("value", [None, [], "bad", {"store_id": "other"}])
def test_pull_bad_contract_fails_closed(setup, value):
    database, *_ = setup
    bridge = Bridge(database, lambda: NOW, base_url="https://trade.test", service_token="secret",
        service_stores=("store_A",), transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": value})))
    with pytest.raises(AppError) as caught:
        bridge.pull_snapshot("store_A")
    assert caught.value.status == 503
    bridge.close()


def test_pull_uses_fixed_store_bearer_and_no_cookie(setup):
    database, _, _, source = setup
    def reply(request):
        assert request.url.path == "/api/v1/internal/stores/store_A/operations-snapshot"
        assert request.headers["Authorization"] == "Bearer secret"
        assert "cookie" not in request.headers
        return httpx.Response(200, json={"data": source})
    bridge = Bridge(database, lambda: NOW, base_url="https://trade.test", service_token="secret",
        service_stores=("store_A",), transport=httpx.MockTransport(reply))
    assert bridge.pull_snapshot("store_A") == source
    with pytest.raises(AppError):
        bridge.pull_snapshot("other")
    bridge.close()


def test_sku_channel_filter_uses_native_line_paid_and_keeps_refund_unknown(setup):
    _, operations, principal, source = setup
    source = deepcopy(source)
    source["revision"] = 2
    other = deepcopy(source["skus"][0])
    other["sku_id"] = "B"
    source["skus"].append(other)
    source["orders"] = [{"order_id": "O1", "paid_at": (NOW-timedelta(hours=1)).isoformat(), "status": "paid",
        "channel": "web", "paid_minor": "30000", "items": [
            {"sku_id": "A", "qty": 1, "paid_minor": "10000"},
            {"sku_id": "B", "qty": 2, "paid_minor": "20000"}]}]
    source["refunds"] = [{"refund_id": "R1", "order_id": "O1", "occurred_at": (NOW-timedelta(minutes=30)).isoformat(), "status": "succeeded", "amount_minor": "5000"}]
    operations.import_snapshot(principal, source)
    result = operations.dashboard(principal, source["snapshot_id"], sku_id="A", channel="web")
    assert result["metrics"]["payment_minor"] == "10000"
    assert result["metrics"]["paid_order_count"] == 1
    assert result["metrics"]["refund_minor"] is None
    assert result["filter_missing"]
    with pytest.raises(AppError):
        operations.dashboard(principal, sku_id="foreign")
    assert operations.assistant(principal, {"question": "这个商品销售如何"})["status"] == "clarify"
