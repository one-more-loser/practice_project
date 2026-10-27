"""有界真实API兼容验证：仅deepseek-flash、合成文本、无业务写入。"""

import json
import time
from datetime import UTC, datetime

import httpx
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.core.errors import AppError
from cs_agent.models.gateway import ModelGateway, ServiceConfig


class Recorder(httpx.BaseTransport):
    def __init__(self):
        self.inner = httpx.HTTPTransport()
        self.items = []

    def handle_request(self, request):
        if request.url.host != "api.deepseek.com" or len(self.items) >= 6:
            raise RuntimeError("approved endpoint or call budget exceeded")
        body = json.loads(request.content)
        if body.get("model") != "deepseek-flash":
            raise RuntimeError("unapproved generation model")
        began = time.perf_counter()
        response = self.inner.handle_request(request)
        response.read()
        meta = {
            "status": response.status_code,
            "request_model": body["model"],
            "seconds": round(time.perf_counter() - began, 3),
        }
        if response.status_code == 200 and not body.get("stream"):
            raw = response.json()
            meta.update(served_model=raw.get("model"), usage=raw.get("usage"))
        self.items.append(meta)
        return response

    def close(self):
        self.inner.close()


def main():
    settings = Settings()
    if settings.chat_api_key is None:
        raise RuntimeError("DeepSeek key not configured; value is never printed")
    config = ServiceConfig(
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        protocol="deepseek-chat",
        service_revision="deepseek-flash-alias-2026-10-23-nonthinking",
        secret_ref="chat_api_key",
        timeout_seconds=20,
        concurrency=1,
    )
    recorder = Recorder()
    gateway = ModelGateway(
        {"chat": config, "sql": config}, settings=settings, transport=recorder
    )
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "purpose": "API compatibility only; no business write or quality claim",
        "checks": {},
        "requests": recorder.items,
    }
    try:
        answer = gateway.invoke(
            "chat",
            {
                "messages": [
                    {
                        "role": "system",
                        "content": '你是模拟店铺客服，仅输出JSON：{"kind":"answer","text":"回答"}。店内仅使用模拟余额，不接真实资金。',
                    },
                    {"role": "user", "content": "这里充值会扣我的银行卡吗？"},
                ]
            },
            remaining=20,
        )
        parsed = json.loads(answer.content)
        assert parsed.get("kind") == "answer" and parsed.get("text")
        report["checks"]["structured_chat"] = "passed"
        result = gateway.invoke(
            "sql",
            {"question": "查询本人最近5个订单的订单号和状态", "parameter_types": {}},
            remaining=20,
        )
        assert result["kind"] in {"query", "clarify"}
        report["checks"]["sql_contract_candidate_only"] = result["kind"]
        streamed = gateway.chat_stream(
            {
                "messages": [{"role": "user", "content": "请用一句话说你好"}],
                "tools": [],
            },
            remaining=20,
        )
        assert streamed.content
        report["checks"]["stream_assembled_finish"] = "passed"
        report["status"] = "passed"
    except (AppError, ValueError, AssertionError, KeyError, TypeError, RuntimeError, httpx.HTTPError) as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
        report["failure_code"] = getattr(exc, "code", None)
    finally:
        recorder.close()
        (PROJECT_ROOT / ".runtime/deepseek-flash-smoke.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
