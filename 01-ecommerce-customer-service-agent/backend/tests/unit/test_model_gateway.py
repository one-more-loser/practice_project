import json

import httpx
import pytest

from cs_agent.core.errors import AppError


def gateway(capability, body, *, status=200):
    from cs_agent.models.gateway import ModelGateway, ServiceConfig

    config = ServiceConfig(
        base_url="https://model.test/v1",
        model="fixture-model",
        service_revision="fixture-v1",
        protocol="chat-completions" if capability in {"chat", "sql"} else capability,
    )
    return ModelGateway(
        {capability: config},
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
    )


def test_embedding_count_dimension_finite_and_model_revision():
    body = {"data": [{"index": 0, "embedding": [0.01] * 1024}], "model": "fixture-model"}
    assert (
        len(gateway("embedding", body).invoke("embedding", {"texts": ["退货政策"]})["vectors"][0])
        == 1024
    )
    for vector in [[1.0] * 3, [float("nan")] * 1024]:
        with pytest.raises(AppError):
            gateway("embedding", {"data": [{"index": 0, "embedding": vector}]}).invoke(
                "embedding", {"texts": ["x"]}
            )


@pytest.mark.parametrize(
    "results",
    [
        [{"candidate_id": "other", "score": 0.5}],
        [{"candidate_id": "a", "score": 0.5}, {"candidate_id": "a", "score": 0.2}],
        [{"candidate_id": "a", "score": 0.5, "score_type": "probability"}],
    ],
)
def test_rerank_does_not_accept_unknown_duplicate_or_undeclared_score(results):
    with pytest.raises(AppError):
        gateway("rerank", {"results": results}).invoke(
            "rerank", {"query": "退款", "candidates": [{"id": "a", "text": "规则"}], "top_n": 1}
        )


def test_sql_query_contract_has_no_execution_and_wrong_version_rejected():
    from cs_agent.query.schema import DIGEST, METRICS_REVISION, REVISION

    candidate = {
        "contract_version": "XCS-SQL-v0.1",
        "kind": "query",
        "schema_revision": REVISION,
        "schema_digest": DIGEST,
        "metric_revision": METRICS_REVISION,
        "sql": "SELECT order_id FROM cs_order_self",
        "parameter_names": [],
        "service": {"model_identifier": "fixture-model", "revision": "fixture-v1"},
    }
    body = {"choices": [{"message": {"content": json.dumps(candidate)}}]}
    assert gateway("sql", body).invoke("sql", {"question": "我的订单"})["kind"] == "query"
    candidate["schema_revision"] = "unknown"
    with pytest.raises(AppError):
        gateway("sql", {"choices": [{"message": {"content": json.dumps(candidate)}}]}).invoke(
            "sql", {"question": "x"}
        )


def test_chat_stream_assembles_tool_json_before_execution():
    from cs_agent.models.gateway import ModelGateway, ServiceConfig

    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "wallet", "arguments": '{"'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'scope":"self"}'}}]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]

    def transport(request):
        data = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(
            200, content=data.encode(), headers={"Content-Type": "text/event-stream"}
        )

    config = ServiceConfig(
        base_url="https://model.test/v1",
        model="fixture-model",
        service_revision="fixture-v1",
        protocol="chat-completions",
    )
    g = ModelGateway({"chat": config}, transport=httpx.MockTransport(transport))
    output = g.chat_stream({"messages": [{"role": "user", "content": "查余额"}], "tools": []})
    assert output.tool_calls == [
        {"name": "wallet", "args": {"scope": "self"}, "id": "call_1", "type": "tool_call"}
    ]
    chunks[-2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = "bad-json"
    with pytest.raises(AppError):
        g.chat_stream({"messages": [], "tools": []})


def test_transient_retry_is_bounded_and_errors_redacted():
    from cs_agent.models.gateway import ModelGateway, ServiceConfig

    calls = []

    def respond(request):
        calls.append(1)
        return httpx.Response(429, json={"secret": "MUST_NOT_LEAK"}, headers={"Retry-After": "0"})

    config = ServiceConfig(
        base_url="https://model.test/v1",
        model="fixture-model",
        service_revision="fixture-v1",
        protocol="chat-completions",
    )
    with pytest.raises(AppError) as exc:
        ModelGateway({"chat": config}, transport=httpx.MockTransport(respond)).invoke(
            "chat", {"messages": []}
        )
    assert len(calls) == 2 and "MUST_NOT_LEAK" not in str(exc.value)
