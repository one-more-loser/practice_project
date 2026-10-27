from fastapi.testclient import TestClient

from cs_agent.config import Settings
from cs_agent.main import create_app


def test_unconfigured_database_does_not_report_ready_or_models_available():
    settings = Settings(
        database_url=None, chat_api_key="test-key-private", sql_api_key="sql-private"
    )
    assert "test-key-private" not in repr(settings)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        result = client.get("/health/ready")
        assert result.status_code == 503
        assert result.json()["data"]["database"] == "unready"
        assert result.json()["data"]["chat_model"] == "unvalidated"
        assert "test-key-private" not in result.text
        assert client.get("/api/v1/session").status_code == 503


def test_error_envelope_has_server_request_id_and_never_echoes_path():
    with TestClient(create_app(Settings(database_url=None))) as client:
        result = client.get(
            "/13800123456/unknown?secret=not-for-log",
            headers={"X-Request-ID": "customer-controlled-secret"},
        )
        assert result.status_code == 404
        body = result.json()
        assert body["contract_version"] == "API-CS-v0.1"
        assert body["request_id"].startswith("req_")
        assert body["error"] == {
            "code": "not_found",
            "message": "资源不存在或不可访问",
            "retryable": False,
        }
        assert "13800123456" not in result.text
        assert "customer-controlled-secret" not in result.text
