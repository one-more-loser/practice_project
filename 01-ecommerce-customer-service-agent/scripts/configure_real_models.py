"""只生成受限服务配置，不复制环境密钥，不覆盖未知的后续SQL配置。"""

import json

import httpx
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.models.gateway import ServiceConfig


def main():
    root = PROJECT_ROOT
    references = json.loads(
        (root / ".runtime/qwen-models.json").read_text(encoding="utf8")
    )["models"]
    health = httpx.get("http://127.0.0.1:8010/health", timeout=3, trust_env=False)
    health.raise_for_status()
    status = health.json()
    if status.get("real_model") is not True or status.get("models") != {
        k: v["revision"] for k, v in references.items()
    }:
        raise RuntimeError("local models do not match pinned references")
    if Settings().chat_api_key is None:
        raise RuntimeError("DeepSeek environment key missing")
    remote = {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "service_revision": "deepseek-flash-alias-2026-10-23-nonthinking",
        "protocol": "deepseek-chat",
        "secret_ref": "chat_api_key",
        "timeout_seconds": 12,
        "concurrency": 1,
    }
    result = {
        "chat": remote,
        "sql": {**remote, "timeout_seconds": 8, "secret_ref": "sql_api_key"},
        "embedding": {
            "base_url": "http://127.0.0.1:8010/v1",
            "model": references["embedding"]["repository"],
            "service_revision": references["embedding"]["revision"]
            + "-cs-qwen-local-v1-fp16-1024",
            "protocol": "qwen-embeddings",
            "timeout_seconds": 5,
            "concurrency": 1,
        },
        "rerank": {
            "base_url": "http://127.0.0.1:8010/v1",
            "model": references["rerank"]["repository"],
            "service_revision": references["rerank"]["revision"]
            + "-cs-qwen-local-v1-fp16",
            "protocol": "rerank-index",
            "score_type": "provider_relevance",
            "timeout_seconds": 5,
            "concurrency": 1,
        },
    }
    for row in result.values():
        ServiceConfig.model_validate(row)
    path = root / ".runtime/model-services.json"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf8"))
        if any(
            previous.get(k, {}).get("model") not in {None, "deepseek-flash"}
            for k in ("chat", "sql")
        ):
            raise RuntimeError("preserving non-temporary generation configuration")
        backup = root / ".runtime/model-services.before-qwen.json"
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
    pending = path.with_suffix(".json.new")
    pending.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    pending.replace(path)
    print("four real capabilities configured; environment key referenced, not copied")


if __name__ == "__main__":
    main()
