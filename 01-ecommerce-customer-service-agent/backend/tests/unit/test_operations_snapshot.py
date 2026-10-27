import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql

from cs_agent.core.errors import AppError
from cs_agent.operations_snapshot.api import require_service
from cs_agent.operations_snapshot.projection import (
    build_projection,
    read_inputs,
    snapshot_identity,
)
from cs_agent.operations_snapshot.service import source_statement

NOW = datetime(2026, 10, 23, tzinfo=UTC)
SCOPE = {"store_id": "store_sim", "data_batch_id": "test_batch", "batch_generation": 1,
         "source_kind": "synthetic"}


def rows():
    return [
        {"kind": "skus", "body": {"sku_id": "sku_gold", "title": "合成商品", "category": "测试",
                                   "base_price_minor": 10000, "cost_minor": None,
                                   "price_version": 1, "available_qty": 100}},
        {"kind": "orders", "body": {"order_id": "old_paid", "paid_at": NOW - timedelta(days=40),
                                     "trade_status": "closed_refunded", "paid_minor": 18400}},
        {"kind": "items", "body": {"order_item_id": "line_a", "order_id": "old_paid",
                                    "sku_id": "sku_gold", "qty": 2, "paid_minor": 18400}},
        {"kind": "refunds", "body": {"refund_id": "aftersale:refund_a", "order_id": "old_paid",
                                      "occurred_at": NOW, "amount_minor": 9200}},
    ]


def test_snapshot_keeps_original_payment_and_cross_period_refund():
    payload = build_projection(SCOPE, rows(), NOW)
    assert payload["orders"][0]["status"] == "paid"
    assert payload["orders"][0]["paid_minor"] == "18400"
    assert payload["refunds"][0]["amount_minor"] == "9200"
    assert payload["skus"][0]["cost_minor"] is None
    assert payload["skus"][0]["variable_fee_minor"] is None
    assert set(payload["source_metadata"]) == {
        "producer", "source_revision", "cost_basis", "capacity_basis"}
    assert "owner_id" not in json.dumps(payload, ensure_ascii=False)


def test_snapshot_matches_real_02_strict_validator():
    source = Path(__file__).resolve().parents[4] / "02-ecommerce-operations-agent/backend/src/ops_agent/domain.py"
    if not source.exists():
        pytest.skip("02 peer validator not available in standalone checkout")
    spec = importlib.util.spec_from_file_location("ops_snapshot_contract", source)
    domain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(domain)
    assert domain.validate_snapshot(build_projection(SCOPE, rows(), NOW))["orders"][0]["paid_minor"] == "18400"


def test_snapshot_identity_stable_and_scoped_to_generation():
    identity = snapshot_identity("store", "batch", 1)
    assert identity == snapshot_identity("store", "batch", 1)
    assert len(identity) <= 80
    assert identity != snapshot_identity("store", "batch", 2)
    assert identity != snapshot_identity("other", "batch", 1)


def test_non_native_freight_allocation_is_rejected():
    values = rows()
    values[1]["body"]["paid_minor"] += 100
    with pytest.raises(AppError) as error:
        build_projection(SCOPE, values, NOW)
    assert error.value.code == "snapshot_source_gap"


def test_refunds_cannot_exceed_original_payment():
    values = rows()
    values[-1]["body"]["amount_minor"] = 18401
    with pytest.raises(AppError):
        build_projection(SCOPE, values, NOW)


def test_future_refunds_are_not_in_current_watermark():
    values = rows()
    values[-1]["body"]["occurred_at"] = NOW + timedelta(seconds=1)
    assert not build_projection(SCOPE, values, NOW)["refunds"]


def test_service_auth_missing_wrong_identity_and_cross_store():
    settings = SimpleNamespace(activity_service_token=None, activity_service_stores=())
    with pytest.raises(AppError) as error:
        require_service(settings, "", "store_sim")
    assert error.value.status == 503
    settings.activity_service_token = SecretStr("test-secret")
    settings.activity_service_stores = ("store_sim",)
    with pytest.raises(AppError) as error:
        require_service(settings, "Bearer wrong", "store_sim")
    assert error.value.status == 401
    with pytest.raises(AppError) as error:
        require_service(settings, "Bearer test-secret", "other")
    assert error.value.status == 404
    require_service(settings, "Bearer test-secret", "store_sim")


def test_explicit_synthetic_inputs_are_scoped_and_fingerprinted(tmp_path):
    path = tmp_path / "synthetic-inputs.json"
    value = {"schema_version": "OPS-SYNTHETIC-INPUTS-v1", "source_kind": "synthetic",
             "inputs_version": "gold-v1", **{k: SCOPE[k] for k in (
                 "store_id", "data_batch_id", "batch_generation")},
             "skus": {"sku_gold": {"cost_minor": 6000, "variable_fee_minor": 500}},
             "capacity_basis": "synthetic staffing assumptions gold-v1"}
    path.write_text(json.dumps(value), encoding="utf-8")
    inputs = read_inputs(path, "store_sim", "test_batch", 1)
    payload = build_projection(SCOPE, rows(), NOW, inputs)
    assert payload["skus"][0]["cost_minor"] == "6000"
    assert payload["skus"][0]["variable_fee_minor"] == "500"
    assert inputs["sha256"] in payload["source_metadata"]["cost_basis"]
    with pytest.raises(AppError):
        read_inputs(path, "other", "test_batch", 1)
    with pytest.raises(AppError):
        build_projection(SCOPE | {"source_kind": "business_import"}, rows(), NOW, inputs)


def test_source_read_is_one_mvcc_statement_without_personal_columns():
    sql = str(source_statement("store_sim").compile(dialect=postgresql.dialect()))
    assert "UNION ALL" in sql and "snapshot_scope" in sql
    for personal in ("owner_id", "recipient", "phone", "street", "password"):
        assert personal not in sql
