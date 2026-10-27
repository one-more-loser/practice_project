import json

import pytest

from cs_agent import runtime_profiles


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_profiles, "PROJECT_ROOT", tmp_path)
    for key in runtime_profiles.DATABASE_KEYS | {"CS_DATABASE_PROFILE"}:
        monkeypatch.delenv(key, raising=False)
    folder = tmp_path / ".runtime"
    folder.mkdir()
    (folder / "local.env").write_text('CS_DATABASE_URL="local-test-value"\n', encoding="utf-8")
    (folder / "debian.env").write_text(
        'CS_DATABASE_URL="debian-test-value"\nCS_ISOLATION_FILE="remote-marker"\nUNRELATED_SECRET="must-not-load"\n',
        encoding="utf-8",
    )
    return folder


def test_active_profile_selects_debian_and_does_not_silently_fall_back(profiles):
    (profiles / "active-database.json").write_text(json.dumps({"profile": "debian"}))
    selected, values = runtime_profiles.profile_values()
    assert selected == "debian" and values["CS_DATABASE_URL"] == "debian-test-value"
    assert "UNRELATED_SECRET" not in values


def test_explicit_local_resets_remote_target_environment(profiles, monkeypatch):
    monkeypatch.setenv("CS_ISOLATION_FILE", "remote-marker")
    runtime_profiles.apply_runtime_profile("local")
    import os

    assert os.environ["CS_DATABASE_URL"] == "local-test-value"
    assert os.environ["CS_ISOLATION_FILE"].endswith("isolation.json")


def test_missing_remote_profile_rejects_instead_of_using_local(profiles):
    (profiles / "debian.env").unlink()
    with pytest.raises(RuntimeError, match="not configured"):
        runtime_profiles.profile_values("debian")


def test_unknown_profile_is_rejected(profiles):
    with pytest.raises(RuntimeError, match="unknown"):
        runtime_profiles.profile_values("../another-project")
