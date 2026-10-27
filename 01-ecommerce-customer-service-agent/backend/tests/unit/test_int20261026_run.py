"""Run entry safety without network, database connections or real private files."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import int20261026_common as common
from int20261026_serve import FileClock


def test_clock_requires_forward_aware_run_identity(tmp_path):
    clock_file = tmp_path / "clock.json"
    clock_file.write_text(json.dumps({"integration_run_id": common.RUN, "now": common.INITIAL_TIME}))
    clock = FileClock(clock_file)
    first = clock.now()
    clock_file.write_text(json.dumps({"integration_run_id": common.RUN, "now": "2026-10-26T05:00:00+00:00"}))
    assert clock.now() > first
    clock_file.write_text(json.dumps({"integration_run_id": common.RUN, "now": common.INITIAL_TIME}))
    with pytest.raises(RuntimeError, match="backward"):
        clock.now()


@pytest.mark.parametrize("run,now", [("other", common.INITIAL_TIME), (common.RUN, "2026-10-26T04:00:00"),
                                   (common.RUN, "2026-10-26T03:59:59+00:00")])
def test_invalid_clock_rejected(tmp_path, run, now):
    path = tmp_path / "clock.json"
    path.write_text(json.dumps({"integration_run_id": run, "now": now}))
    with pytest.raises(RuntimeError):
        FileClock(path).now()


def test_run_environment_cannot_inherit_legacy_target_or_model(monkeypatch):
    monkeypatch.setenv("CS_DATABASE_URL", "old-target")
    monkeypatch.setenv("CS_CHAT_API_KEY", "fixture-never-real")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-never-real")
    fixture = {"passwords": {key: "test-password" for key in common.ROLES}, "service_token": "test-token"}
    monkeypatch.setattr(common, "configuration", lambda: fixture)
    common.apply_cs()
    import os

    from sqlalchemy.engine import make_url
    assert make_url(os.environ["CS_DATABASE_URL"]).database == common.DATABASES["cs"]
    assert make_url(os.environ["CS_DATABASE_URL"]).username == common.ROLES["cs_app"]
    assert os.environ["CS_ISOLATION_FILE"] == str(common.PRIVATE / "isolation.json")
    assert "CS_CHAT_API_KEY" not in os.environ and "DEEPSEEK_API_KEY" not in os.environ
    assert all(os.environ[key] == "false" for key in
               ("CS_AGENT_WORKER_ENABLED", "CS_EXPIRATION_WORKER_ENABLED", "CS_ACTIVITY_DELIVERY_WORKER_ENABLED"))


@pytest.mark.parametrize("field", ["integration_run_id", "roles", "databases"])
def test_wrong_allocation_cannot_be_loaded(monkeypatch, field):
    cfg = {"integration_run_id": common.RUN, "roles": common.ROLES, "databases": common.DATABASES}
    cfg[field] = "wrong"
    monkeypatch.setattr(common, "read", lambda _: cfg)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        common.configuration()
