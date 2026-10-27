"""活动公开读取边界；纯内存SQL结果适配器，不接PG或模型。"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cs_agent.activities import service
from cs_agent.activities.contracts import manifest_digest
from cs_agent.core.errors import AppError

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
PUBLIC_KEYS = {
    "activity_id",
    "intent_id",
    "plan_id",
    "plan_version",
    "phase",
    "revision",
    "start_at",
    "end_at",
}


def activity():
    return dict(
        activity_id="act_test",
        intent_id="intent_test",
        phase="active",
        revision=3,
        start_at=datetime(2026, 10, 23, tzinfo=UTC),
        end_at=datetime(2026, 10, 24, tzinfo=UTC),
        reserved_discount_minor=800,
        consumed_discount_minor=1600,
        manifest=dict(
            plan_id="plan_test",
            plan_version=2,
            start_at="2026-10-23T00:00:00Z",
            end_at="2026-10-24T00:00:00Z",
            budget_cap_minor="100000",
            fixed_fee_minor="10000",
            variable_budget_cap_minor="90000",
            lines=[
                dict(
                    sku_id="sku_test",
                    base_price_minor="10000",
                    activity_price_minor="9200",
                    quantity_cap=100,
                    price_version=1,
                )
            ],
            future_nested={"fixed_fee_minor": "10000"},
        ),
    )


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class MemorySession:
    def __init__(self, row):
        self.row, self.info, self.queries = row, {}, []

    def execute(self, statement):
        self.queries.append(statement)
        if statement.get_final_froms()[0].name == "activity_execution":
            return Rows([self.row] if self.row else [])
        return Rows(
            [
                dict(
                    sku_id="sku_test",
                    base_price_minor=10000,
                    effective_price_minor=9200,
                    quantity_cap=100,
                    reserved_qty=1,
                    consumed_qty=2,
                )
            ]
        )


@pytest.fixture
def reader(monkeypatch):
    state = SimpleNamespace(capabilities=("metrics.read",), row=activity())

    def require(session, ctx, capability, now, **kwargs):
        if capability not in state.capabilities:
            raise AppError("forbidden", 403, "当前身份不具备运营能力")
        return state

    monkeypatch.setattr(service, "require_operations", require)
    # 处于窗口内的active行，真实refresh_phase无需写入。
    ctx = SimpleNamespace(store_id="store_test", data_batch_id="batch_test", batch_generation=1)

    def read(detail=False):
        session = MemorySession(state.row)
        result = service.list_activities(session, ctx, NOW, "act_test" if detail else None)
        return (result if detail else result["items"][0]), session

    return state, read, ctx


@pytest.mark.parametrize("detail", [False, True])
def test_metrics_only_is_allowlisted_no_manifest_money_or_quantity_inference(reader, detail):
    state, read, _ = reader
    original, digest = deepcopy(state.row), manifest_digest(state.row["manifest"])
    item, session = read(detail)
    assert set(item) == PUBLIC_KEYS
    assert state.row == original
    assert manifest_digest(state.row["manifest"]) == digest
    assert len(session.queries) == 1  # 不查询成本行，也不按活动追加授权查询。


@pytest.mark.parametrize("detail", [False, True])
def test_cost_reader_keeps_complete_original_projection(reader, detail):
    state, read, _ = reader
    state.capabilities = ("metrics.read", "cost.read")
    item, session = read(detail)
    assert item["manifest"] == state.row["manifest"]
    assert item["reserved_discount_minor"] == "800"
    assert item["consumed_discount_minor"] == "1600"
    if detail:
        assert item["lines"][0]["quantity_cap"] == 100
        assert item["lines"][0]["effective_price_minor"] == "9200"
    assert len(session.queries) == (2 if detail else 1)


@pytest.mark.parametrize("detail", [False, True])
def test_cost_revocation_reprojects_same_row_on_next_read(reader, detail):
    state, read, _ = reader
    state.capabilities = ("metrics.read", "cost.read")
    assert "manifest" in read(detail)[0]
    state.capabilities = ("metrics.read",)
    assert set(read(detail)[0]) == PUBLIC_KEYS


@pytest.mark.parametrize("capabilities", [(), ("cost.read",)])
def test_metrics_permission_still_required(reader, capabilities):
    state, read, _ = reader
    state.capabilities = capabilities
    with pytest.raises(AppError) as error:
        read()
    assert error.value.status == 403


def test_missing_detail_uses_fixed_not_found_without_manifest(reader):
    state, read, _ = reader
    state.row = None
    with pytest.raises(AppError) as error:
        read(True)
    assert (error.value.code, error.value.status) == ("not_found", 404)
    assert "100000" not in str(error.value)


@pytest.mark.parametrize("detail", [False, True])
def test_real_api_entry_returns_permission_projected_service_data(reader, monkeypatch, detail):
    # main是稳定生产导入入口，避免snapshot.api单独采集的已知循环。
    import cs_agent.main  # noqa: F401
    from cs_agent.activities import api

    state, _, ctx = reader
    session = MemorySession(state.row)

    @contextmanager
    def transaction():
        yield session

    monkeypatch.setattr(api, "auth", lambda *args, **kwargs: ctx)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                db=SimpleNamespace(transaction=transaction), clock=SimpleNamespace(now=lambda: NOW)
            )
        ),
        state=SimpleNamespace(request_id="unit-privacy"),
    )
    result = api.activity_detail("act_test", request) if detail else api.list_activities(request)
    item = result["data"] if detail else result["data"]["items"][0]
    assert set(item) == PUBLIC_KEYS
    assert result["request_id"] == "unit-privacy"
