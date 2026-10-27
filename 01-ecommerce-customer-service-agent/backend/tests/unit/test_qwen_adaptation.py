import json

import httpx
import pytest
from pydantic import ValidationError

from cs_agent.config import Settings
from cs_agent.models.gateway import ModelGateway, ServiceConfig


def test_deepseek_environment_alias_and_explicit_cs_key_precedence(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-shared")
    monkeypatch.delenv("CS_CHAT_API_KEY", raising=False)
    monkeypatch.delenv("CS_SQL_API_KEY", raising=False)
    assert Settings().chat_api_key.get_secret_value() == "fixture-shared"
    assert Settings().sql_api_key.get_secret_value() == "fixture-shared"
    monkeypatch.setenv("CS_CHAT_API_KEY", "fixture-chat")
    assert Settings().chat_api_key.get_secret_value() == "fixture-chat"


def test_qwen_query_document_purpose_is_explicit_on_wire():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 1024}]})

    config = ServiceConfig(
        base_url="http://127.0.0.1:8010/v1",
        model="Qwen/Qwen3-Embedding-0.6B",
        protocol="qwen-embeddings",
        service_revision="fixture-qwen",
    )
    gateway = ModelGateway({"embedding": config}, transport=httpx.MockTransport(respond))
    for purpose in ["query", "document"]:
        gateway.invoke("embedding", {"texts": ["订单改址政策"], "purpose": purpose})
    assert [r["input_type"] for r in requests] == ["query", "document"]
    assert all(r["dimensions"] == 1024 for r in requests)


def test_deepseek_flash_explicit_non_thinking_and_output_policy():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"kind":"answer","text":"你好"}'}}]}
        )

    config = ServiceConfig(
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        protocol="deepseek-chat",
        service_revision="fixture-service",
    )
    gateway = ModelGateway({"chat": config}, transport=httpx.MockTransport(respond))
    gateway.invoke("chat", {"messages": [{"role": "user", "content": "用JSON回答"}]})
    assert requests[0]["model"] == "deepseek-flash"
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["response_format"] == {"type": "json_object"}
    with pytest.raises(ValidationError):
        ServiceConfig(
            base_url="https://api.deepseek.com",
            model="other-model",
            protocol="deepseek-chat",
            service_revision="x",
        )


def test_deepseek_sql_adapter_wraps_omitted_metadata_but_rejects_conflicting_schema():
    from cs_agent.core.errors import AppError
    from cs_agent.query.schema import REVISION

    response = {"kind": "clarify", "question": "请提供日期范围"}

    def respond(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(response)}}]}
        )

    config = ServiceConfig(
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        protocol="deepseek-chat",
        service_revision="fixture",
    )
    gateway = ModelGateway({"sql": config}, transport=httpx.MockTransport(respond))
    assert gateway.invoke("sql", {"question": "订单统计"})["schema_revision"] == REVISION
    response["schema_revision"] = "conflicting-schema"
    with pytest.raises(AppError):
        gateway.invoke("sql", {"question": "订单统计"})
