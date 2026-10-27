"""真实本机Qwen/HTTP分层验证，不调用生成API或写业务库。"""

import hashlib
import json
import math
import statistics
import time

import httpx
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.models.gateway import ModelGateway


def dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def main():
    path = PROJECT_ROOT / "tests/evaluation/rag/dataset.json"
    dataset = json.loads(path.read_text(encoding="utf8"))
    gateway = ModelGateway.from_settings(Settings())
    sources = dataset["sources"]
    health = httpx.get(
        "http://127.0.0.1:8010/health", timeout=3, trust_env=False
    ).json()
    assert health.get("real_model") is True
    corpus = gateway.invoke(
        "embedding",
        {
            "texts": [row["title"] + "\n" + row["body"] for row in sources],
            "purpose": "document",
        },
        remaining=20,
    )["vectors"]
    assert all(len(v) == 1024 and abs(math.sqrt(dot(v, v)) - 1) < 1e-3 for v in corpus)
    cases = []
    for row in dataset["answerable"]:
        began = time.perf_counter()
        query = gateway.invoke(
            "embedding", {"texts": [row["question"]], "purpose": "query"}, remaining=8
        )["vectors"][0]
        dense = sorted(range(len(sources)), key=lambda i: (-dot(query, corpus[i]), i))
        candidates = [
            {
                "id": sources[i]["source_id"],
                "text": sources[i]["title"] + "\n" + sources[i]["body"],
            }
            for i in dense
        ]
        reranked = gateway.invoke(
            "rerank",
            {
                "query": row["question"],
                "candidates": candidates,
                "top_n": len(candidates),
            },
            remaining=6,
        )
        ids = [r["id"] for r in reranked["results"]]
        cases.append(
            {
                "id": row["id"],
                "gold_source_id": row["gold_source_id"],
                "dense_rank": next(
                    j + 1
                    for j, i in enumerate(dense)
                    if sources[i]["source_id"] == row["gold_source_id"]
                ),
                "reranked_rank": ids.index(row["gold_source_id"]) + 1,
                "seconds": round(time.perf_counter() - began, 4),
            }
        )
        if len(cases) % 10 == 0:
            print(f"real local Qwen: {len(cases)}/70 regression questions", flush=True)
    report = {
        "scope": "existing synthetic regression; seven source documents, no unseen holdout or full corpus/load claim",
        "dataset_id": dataset["dataset_id"],
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_count": len(sources),
        "question_count": len(cases),
        "negative_routing": "not scored by vector similarity; 30 application routing negatives require Agent/business validation",
        "dense_top1": statistics.mean(c["dense_rank"] == 1 for c in cases),
        "dense_top3": statistics.mean(c["dense_rank"] <= 3 for c in cases),
        "reranked_top1": statistics.mean(c["reranked_rank"] == 1 for c in cases),
        "reranked_top3": statistics.mean(c["reranked_rank"] <= 3 for c in cases),
        "reranked_mrr": statistics.mean(1 / c["reranked_rank"] for c in cases),
        "p95_seconds": sorted(c["seconds"] for c in cases)[
            math.ceil(0.95 * len(cases)) - 1
        ],
        "health_after": httpx.get(
            "http://127.0.0.1:8010/health", timeout=3, trust_env=False
        ).json(),
        "cases": cases,
    }
    (PROJECT_ROOT / ".runtime/qwen-local-evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
