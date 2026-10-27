import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from model_service.qwen_service import EMBEDDING, RERANKER, create_app


class ProtocolEngine:
    def __init__(self):
        self.calls = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def health(self):
        return {"status": "ready", "device": "protocol-double", "real_model": False}

    def embed(self, texts, purpose):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.015)
        self.calls.append((texts, purpose))
        with self.lock:
            self.active -= 1
        return [[0.1] * 1024 for _ in texts]

    def rerank(self, query, documents):
        return [0.2, 0.8][: len(documents)]


def test_local_embedding_purpose_bounds_and_validation_does_not_echo_input():
    engine = ProtocolEngine()
    with TestClient(create_app(engine)) as client:
        body = {"model": EMBEDDING, "input": ["政策"], "input_type": "query", "dimensions": 1024}
        result = client.post("/v1/embeddings", json=body)
        assert result.status_code == 200
        assert engine.calls == [(["政策"], "query")]
        assert len(result.json()["data"][0]["embedding"]) == 1024
        assert client.get("/health").json()["real_model"] is False
        body["input_type"] = "MUST_NOT_ECHO"
        rejected = client.post("/v1/embeddings", json=body)
        assert rejected.status_code == 422 and "MUST_NOT_ECHO" not in rejected.text
        body["input_type"] = "query"
        body["dimensions"] = 512
        assert client.post("/v1/embeddings", json=body).status_code == 422


def test_reranking_returns_input_indexes_and_declared_score_semantics():
    with TestClient(create_app(ProtocolEngine())) as client:
        result = client.post(
            "/v1/rerank",
            json={"model": RERANKER, "query": "退款", "documents": ["A", "B"], "top_n": 1},
        )
        assert result.status_code == 200
        assert result.json()["results"] == [
            {"index": 1, "relevance_score": 0.8, "score_type": "provider_relevance"}
        ]
        assert (
            client.post(
                "/v1/rerank", json={"model": "other", "query": "x", "documents": ["x"]}
            ).status_code
            == 422
        )


def test_embedding_and_reranking_share_one_gpu_execution_gate():
    engine = ProtocolEngine()
    with TestClient(create_app(engine)) as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(
                pool.map(
                    lambda _: client.post(
                        "/v1/embeddings",
                        json={"model": EMBEDDING, "input": ["政策"], "input_type": "document"},
                    ),
                    range(4),
                )
            )
        assert all(r.status_code == 200 for r in responses)
        assert engine.peak == 1
