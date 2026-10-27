import json
import math
import time
from urllib.parse import urlsplit

import httpx
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cs_agent.core.errors import AppError
from cs_agent.privacy.redaction import external_text
from cs_agent.query.metrics import METRICS
from cs_agent.query.schema import DIGEST, METRICS_REVISION, REVISION, SCHEMA


def failure(code="model_protocol_error"):
    return AppError(code, 503, "模型服务不可用或响应不符合能力契约")


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    base_url: str
    model: str
    service_revision: str
    protocol: str
    secret_ref: str | None = None
    timeout_seconds: float = 4
    concurrency: int = 2
    score_type: str | None = None

    @field_validator("model", "service_revision")
    @classmethod
    def nonempty_identifier(cls, value):
        if not value.strip() or len(value) > 160:
            raise ValueError("invalid service identifier")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def bounded_timeout(cls, value):
        if not 0.1 <= value <= 60:
            raise ValueError("timeout outside supported budget")
        return value

    @field_validator("concurrency")
    @classmethod
    def bounded_concurrency(cls, value):
        if not 1 <= value <= 8:
            raise ValueError("concurrency outside supported budget")
        return value

    @model_validator(mode="after")
    def protocol_for_model(self):
        allowed = {
            "chat": {"chat-completions", "deepseek-chat"},
            "sql": {"chat-completions", "deepseek-chat", "project-sql"},
            "embedding": {"embedding", "openai-embeddings", "qwen-embeddings"},
            "rerank": {"rerank", "rerank-index"},
        }
        if self.protocol not in set().union(*allowed.values()):
            raise ValueError("unsupported model protocol")
        if self.protocol == "deepseek-chat" and self.model != "deepseek-flash":
            raise ValueError("only user-approved deepseek-flash is enabled")
        return self

    @field_validator("base_url")
    @classmethod
    def url(cls, value):
        p = urlsplit(value)
        if (
            p.scheme not in {"http", "https"}
            or not p.hostname
            or p.username
            or p.password
            or p.query
            or p.fragment
        ):
            raise ValueError("invalid service URL")
        return value.rstrip("/")


class ModelGateway:
    def __init__(self, configs=None, *, settings=None, transport=None):
        self.configs = configs or {}
        self.settings = settings
        self.transport = transport
        from threading import BoundedSemaphore

        self.limits = {
            key: BoundedSemaphore(max(1, min(8, value.concurrency)))
            for key, value in self.configs.items()
        }

    @classmethod
    def from_settings(cls, settings):
        from cs_agent.config import PROJECT_ROOT

        path = settings.model_services_file.resolve()
        if not path.is_relative_to(PROJECT_ROOT / ".runtime"):
            raise failure("model_configuration_invalid")
        if not path.exists():
            return cls(settings=settings)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not raw.keys() <= {"chat", "sql", "embedding", "rerank"}:
                raise ValueError()
            configs = {key: ServiceConfig.model_validate(value) for key, value in raw.items()}
            return cls(configs, settings=settings)
        except Exception:
            raise failure("model_configuration_invalid") from None

    def _config(self, capability):
        config = self.configs.get(capability)
        if config is None:
            raise failure("model_unconfigured")
        allowed = {
            "chat": {"chat-completions", "deepseek-chat"},
            "sql": {"chat-completions", "deepseek-chat", "project-sql"},
            "embedding": {"embedding", "openai-embeddings", "qwen-embeddings"},
            "rerank": {"rerank", "rerank-index"},
        }
        if config.protocol not in allowed.get(capability, set()) or (
            capability == "embedding" and len(config.model) > 100
        ):
            raise failure("model_configuration_invalid")
        return config

    def _headers(self, config):
        headers = {"Content-Type": "application/json"}
        if config.secret_ref:
            if config.secret_ref not in {
                "chat_api_key",
                "sql_api_key",
                "embedding_api_key",
                "rerank_api_key",
            }:
                raise failure("model_configuration_invalid")
            key = getattr(self.settings, config.secret_ref, None)
            if key is None:
                raise failure("model_unconfigured")
            headers["Authorization"] = "Bearer " + key.get_secret_value()
        return headers

    def _request(self, capability, path, payload, remaining, cancel_event=None):
        if remaining <= 0:
            raise failure("model_timeout")
        config = self._config(capability)
        deadline = time.monotonic() + max(0.01, min(remaining, config.timeout_seconds))
        gate = self.limits[capability]
        if not gate.acquire(timeout=max(0.01, deadline - time.monotonic())):
            raise failure("model_busy")
        try:
            with httpx.Client(
                transport=self.transport, follow_redirects=False, trust_env=False
            ) as client:
                for attempt in range(2):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise failure("model_timeout")
                    try:
                        with client.stream(
                            "POST",
                            config.base_url + path,
                            json=payload,
                            headers=self._headers(config),
                            timeout=left,
                        ) as response:
                            if response.status_code in {429, 502, 503, 504}:
                                if attempt:
                                    raise failure("model_unavailable")
                                try:
                                    delay = min(
                                        0.2,
                                        max(0, float(response.headers.get("Retry-After", ".1"))),
                                    )
                                except ValueError:
                                    delay = 0.1
                                if delay >= deadline - time.monotonic():
                                    raise failure("model_timeout")
                                time.sleep(delay)
                                continue
                            if response.status_code != 200:
                                raise failure("model_unavailable")
                            content = bytearray()
                            for block in response.iter_bytes():
                                if cancel_event is not None and cancel_event.is_set():
                                    raise failure("model_cancelled")
                                content.extend(block)
                                if len(content) > 4 * 1024 * 1024 or time.monotonic() > deadline:
                                    raise failure("model_budget_exceeded")
                            return bytes(content)
                    except (httpx.TimeoutException, httpx.NetworkError):
                        if attempt:
                            raise failure("model_timeout") from None
                raise failure("model_unavailable")
        finally:
            gate.release()

    def invoke(self, capability, payload, remaining=20, *, cancel_event=None):
        config = self._config(capability)
        try:
            if capability in {"chat", "sql"}:
                if not isinstance(payload, dict):
                    raise failure("model_protocol_error")
                if capability == "sql":
                    if (
                        not isinstance(payload.get("question"), str)
                        or len(payload["question"]) > 4000
                    ):
                        raise failure()
                    safe_question = external_text(payload["question"])
                    contract = {
                        "contract_version": "XCS-SQL-v0.1",
                        "question": safe_question,
                        "dialect": "postgresql",
                        "consumer_profile": "customer_self",
                        "schema_revision": REVISION,
                        "schema_digest": DIGEST,
                        "metric_revision": METRICS_REVISION,
                        "resources": SCHEMA,
                        "metric_definitions": METRICS,
                        "parameters": payload.get("parameter_types", {}),
                        "limits": {"max_rows": 50, "read_only": True},
                    }
                    # 兼容v0.1的可选上下文；不接收SQL模型生成的绑定值或身份。
                    if "query_context" in payload:
                        contract["query_context"] = payload["query_context"]
                    if config.protocol == "deepseek-chat":
                        contract["response_contract"] = {
                            "contract_version": "XCS-SQL-v0.1",
                            "kind": "query|clarify|unsupported|error",
                            "schema_revision": REVISION,
                            "schema_digest": DIGEST,
                            "metric_revision": METRICS_REVISION,
                            "sql": "仅query填写带命名参数的只读SQL；资源只限所给视图",
                            "parameter_names": "query填写实际使用的参数名数组，不含冒号",
                        }
                    request = (
                        contract
                        if config.protocol == "project-sql"
                        else {
                            "model": config.model,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": "仅输出符合所给XCS-SQL-v0.1契约的JSON。生成候选，不执行SQL。",
                                },
                                {
                                    "role": "user",
                                    "content": json.dumps(contract, ensure_ascii=False),
                                },
                            ],
                            "response_format": {"type": "json_object"},
                            "max_tokens": 1200,
                        }
                    )
                else:
                    request = {
                        "model": config.model,
                        "messages": payload["messages"],
                        "response_format": {"type": "json_object"},
                        "max_tokens": 1500,
                    }
                    if payload.get("tools"):
                        request["tools"] = payload["tools"]
                if config.protocol == "deepseek-chat":
                    request["thinking"] = {"type": "disabled"}
                path = "/generate" if config.protocol == "project-sql" else "/chat/completions"
                raw = json.loads(self._request(capability, path, request, remaining, cancel_event))
                if capability == "chat":
                    message = raw["choices"][0]["message"]
                    calls = []
                    for call in message.get("tool_calls", []):
                        calls.append(
                            {
                                "name": call["function"]["name"],
                                "id": call["id"],
                                "args": json.loads(call["function"]["arguments"]),
                            }
                        )
                    return AIMessage(content=message.get("content") or "", tool_calls=calls)
                result = (
                    raw
                    if config.protocol == "project-sql"
                    else json.loads(raw["choices"][0]["message"]["content"])
                )
                if config.protocol == "deepseek-chat":
                    # 服务身份来自后端配置，不能由通用模型臆测供应商修订。
                    result["service"] = {
                        "model_identifier": config.model,
                        "revision": config.service_revision,
                    }
                    # 该适配器是XCS服务提供方：给省略的传输元信息绑定本次请求，
                    # 已返回但不一致的修订仍由下方检查拒绝；不补造SQL或业务参数。
                    for key, expected in {
                        "contract_version": "XCS-SQL-v0.1",
                        "schema_revision": REVISION,
                        "schema_digest": DIGEST,
                        "metric_revision": METRICS_REVISION,
                    }.items():
                        result.setdefault(key, expected)
                if (
                    result.get("contract_version") != "XCS-SQL-v0.1"
                    or result.get("schema_revision") != REVISION
                    or result.get("schema_digest") != DIGEST
                    or result.get("metric_revision") != METRICS_REVISION
                    or result.get("kind") not in {"query", "clarify", "unsupported", "error"}
                ):
                    raise failure()
                if result["kind"] == "query" and (
                    not isinstance(result.get("sql"), str)
                    or not isinstance(result.get("parameter_names"), list)
                ):
                    raise failure()
                service = result.get("service", {})
                if not service.get("model_identifier") or not service.get("revision"):
                    raise failure()
                return result
            if capability == "embedding":
                if not isinstance(payload.get("texts"), list) or any(
                    not isinstance(value, str) for value in payload["texts"]
                ):
                    raise failure()
                texts = [external_text(value) for value in payload["texts"]]
                if not 1 <= len(texts) <= 64:
                    raise failure()
                request = {"model": config.model, "input": texts}
                if config.protocol == "qwen-embeddings":
                    purpose = payload.get("purpose")
                    if purpose not in {"query", "document"}:
                        raise failure()
                    request.update(input_type=purpose, dimensions=1024)
                raw = json.loads(
                    self._request(
                        capability,
                        "/embeddings",
                        request,
                        remaining,
                        cancel_event,
                    )
                )
                data = raw["data"]
                if len(data) != len(texts) or sorted(row["index"] for row in data) != list(
                    range(len(texts))
                ):
                    raise failure()
                vectors = [row["embedding"] for row in sorted(data, key=lambda row: row["index"])]
                if any(
                    len(v) != 1024
                    or any(type(n) not in {int, float} or not math.isfinite(n) for n in v)
                    or sum(n * n for n in v) == 0
                    for v in vectors
                ):
                    raise failure()
                return {
                    "vectors": vectors,
                    "dimension": 1024,
                    "model": config.model,
                    "service_revision": config.service_revision,
                }
            if capability == "rerank":
                if not isinstance(payload.get("query"), str) or not isinstance(
                    payload.get("candidates"), list
                ):
                    raise failure()
                candidates = payload["candidates"]
                ids = [row["id"] for row in candidates]
                if len(ids) != len(set(ids)) or len(ids) > 40:
                    raise failure()
                request = {
                    "model": config.model,
                    "query": external_text(payload["query"]),
                    "candidates": [
                        {"id": row["id"], "text": external_text(row["text"])} for row in candidates
                    ],
                    "top_n": min(payload.get("top_n", 5), len(ids)),
                }
                if config.protocol == "rerank-index":
                    request["documents"] = [row["text"] for row in candidates]
                    del request["candidates"]
                raw = json.loads(
                    self._request(capability, "/rerank", request, remaining, cancel_event)
                )
                output = []
                for row in raw["results"]:
                    id_ = (
                        ids[row["index"]]
                        if config.protocol == "rerank-index"
                        and type(row.get("index")) is int
                        and 0 <= row["index"] < len(ids)
                        else row.get("candidate_id")
                    )
                    score = row.get("score", row.get("relevance_score"))
                    score_type = row.get("score_type", config.score_type)
                    if (
                        id_ not in ids
                        or id_ in {r["id"] for r in output}
                        or type(score) not in {int, float}
                        or not math.isfinite(score)
                        or score_type not in {"provider_logit", "provider_relevance"}
                    ):
                        raise failure()
                    output.append({"id": id_, "score": score, "score_type": score_type})
                return {
                    "results": output[: request["top_n"]],
                    "model": config.model,
                    "service_revision": config.service_revision,
                }
            raise failure()
        except AppError:
            raise
        except Exception:
            raise failure() from None

    def chat_stream(self, payload, remaining=20):
        # 缓冲片段并用LangChain消息类型归并；未得到结束帧前不交给工具执行器。
        config = self._config("chat")
        request = {
            "model": config.model,
            "messages": payload["messages"],
            "tools": payload.get("tools", []),
            "stream": True,
            "max_tokens": 1500,
        }
        if config.protocol == "deepseek-chat":
            request["thinking"] = {"type": "disabled"}
        raw = self._request(
            "chat",
            "/chat/completions",
            request,
            remaining,
        )
        try:
            combined = AIMessageChunk(content="")
            ended = False
            for line in raw.decode().splitlines():
                if not line.startswith("data:"):
                    continue
                value = line[5:].strip()
                if value == "[DONE]":
                    break
                choice = json.loads(value)["choices"][0]
                delta = choice.get("delta", {})
                chunks = [
                    {
                        "index": c["index"],
                        "id": c.get("id"),
                        "name": c.get("function", {}).get("name"),
                        "args": c.get("function", {}).get("arguments", ""),
                    }
                    for c in delta.get("tool_calls", [])
                ]
                combined += AIMessageChunk(
                    content=delta.get("content") or "", tool_call_chunks=chunks
                )
                ended = ended or choice.get("finish_reason") in {"stop", "tool_calls"}
            if not ended or combined.invalid_tool_calls:
                raise failure()
            # LangChain的容错部分JSON解析不能作为执行许可；严格再解析完整原始参数。
            for chunk in combined.tool_call_chunks:
                if not chunk.get("id") or not chunk.get("name"):
                    raise failure()
                json.loads(chunk["args"])
            return AIMessage(content=combined.content, tool_calls=combined.tool_calls)
        except AppError:
            raise
        except Exception:
            raise failure() from None
