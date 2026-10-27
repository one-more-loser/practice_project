"""回环地址Qwen服务：固定模型、离线权重、共享GPU队列，不包含业务权限。"""

import asyncio
import hashlib
import json
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parents[1]
EMBEDDING = "Qwen/Qwen3-Embedding-0.6B"
RERANKER = "Qwen/Qwen3-Reranker-0.6B"
INSTRUCTION = "Given an ecommerce customer question, retrieve store policy or product passages that answer the question"
SERVICE_REVISION = "cs-qwen-local-v1"


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    model: Literal["Qwen/Qwen3-Embedding-0.6B"]
    input: list[str] = Field(min_length=1, max_length=64)
    input_type: Literal["query", "document"]
    dimensions: Literal[1024] = 1024


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    model: Literal["Qwen/Qwen3-Reranker-0.6B"]
    query: str = Field(min_length=1, max_length=4000)
    documents: list[str] = Field(min_length=1, max_length=40)
    top_n: int = Field(default=5, ge=1, le=40)


def checked_paths():
    reference = json.loads(
        (ROOT / ".runtime/qwen-models.json").read_text(encoding="utf8")
    )
    shared = (ROOT.parent / "shared-models").resolve()
    paths = {}
    for capability, repo in [("embedding", EMBEDDING), ("rerank", RERANKER)]:
        row = reference["models"][capability]
        path = Path(row["path"]).resolve()
        if row["repository"] != repo or not path.is_relative_to(shared):
            raise RuntimeError("model reference outside approved snapshots")
        manifest_bytes = (path / "verified-model.json").read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != row["manifest_sha256"]:
            raise RuntimeError("model manifest changed")
        manifest = json.loads(manifest_bytes)
        if manifest["revision"] != row["revision"] or manifest["repository"] != repo:
            raise RuntimeError("model revision mismatch")
        for item in manifest["files"]:
            target = (path / item["file"]).resolve()
            if (
                not target.is_relative_to(path)
                or target.stat().st_size != item["bytes"]
            ):
                raise RuntimeError("model snapshot missing or incomplete")
            with target.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != item["sha256"]:
                raise RuntimeError("model snapshot checksum mismatch")
        paths[capability] = (path, row["revision"])
    return paths


class QwenEngine:
    def __init__(self):
        import torch
        from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable; no silent CPU fallback")
        self.torch = torch
        self.paths = checked_paths()
        self.max_length = 1024
        self.embedding_tokenizer = AutoTokenizer.from_pretrained(
            self.paths["embedding"][0],
            local_files_only=True,
            trust_remote_code=False,
            padding_side="left",
        )
        self.embedding_model = (
            AutoModel.from_pretrained(
                self.paths["embedding"][0],
                local_files_only=True,
                trust_remote_code=False,
                dtype=torch.float16,
                attn_implementation="sdpa",
            )
            .to("cuda")
            .eval()
        )
        self.rerank_tokenizer = AutoTokenizer.from_pretrained(
            self.paths["rerank"][0],
            local_files_only=True,
            trust_remote_code=False,
            padding_side="left",
        )
        self.rerank_model = (
            AutoModelForCausalLM.from_pretrained(
                self.paths["rerank"][0],
                local_files_only=True,
                trust_remote_code=False,
                dtype=torch.float16,
                attn_implementation="sdpa",
            )
            .to("cuda")
            .eval()
        )
        prefix = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        self.prefix = self.rerank_tokenizer.encode(prefix, add_special_tokens=False)
        self.suffix = self.rerank_tokenizer.encode(suffix, add_special_tokens=False)
        self.yes = self.rerank_tokenizer.convert_tokens_to_ids("yes")
        self.no = self.rerank_tokenizer.convert_tokens_to_ids("no")

    def health(self):
        torch = self.torch
        return {
            "status": "ready",
            "real_model": True,
            "device": torch.cuda.get_device_name(),
            "dtype": "float16",
            "dimensions": 1024,
            "service_revision": SERVICE_REVISION,
            "models": {key: revision for key, (_, revision) in self.paths.items()},
            "max_length": self.max_length,
            "gpu_concurrency": 1,
            "allocated_mib": round(torch.cuda.memory_allocated() / 1048576, 2),
            "reserved_mib": round(torch.cuda.memory_reserved() / 1048576, 2),
            "peak_reserved_mib": round(torch.cuda.max_memory_reserved() / 1048576, 2),
        }

    def _tokens(self, tokenizer, texts, extra=0):
        rows = tokenizer(
            texts, padding=False, truncation=False, add_special_tokens=False
        )
        if any(len(ids) + extra > self.max_length for ids in rows["input_ids"]):
            raise ValueError("input exceeds encoding budget")
        return rows

    def embed(self, texts, purpose):
        torch = self.torch
        texts = (
            [f"Instruct: {INSTRUCTION}\nQuery:{text}" for text in texts]
            if purpose == "query"
            else texts
        )
        vectors = []
        with torch.inference_mode():
            for start in range(0, len(texts), 4):
                rows = self._tokens(self.embedding_tokenizer, texts[start : start + 4])
                inputs = self.embedding_tokenizer.pad(
                    rows, padding=True, return_tensors="pt"
                ).to("cuda")
                output = self.embedding_model(
                    **inputs, use_cache=False
                ).last_hidden_state[:, -1, :]
                normalized = torch.nn.functional.normalize(output.float(), p=2, dim=1)
                if not torch.isfinite(normalized).all():
                    raise ValueError("non-finite vectors")
                vectors.extend(normalized.cpu().tolist())
        return vectors

    def rerank(self, query, documents):
        torch = self.torch
        scores = []
        with torch.inference_mode():
            for document in documents:
                text = f"<Instruct>: {INSTRUCTION}\n<Query>: {query}\n<Document>: {document}"
                rows = self._tokens(
                    self.rerank_tokenizer, [text], len(self.prefix) + len(self.suffix)
                )
                rows["input_ids"] = [self.prefix + rows["input_ids"][0] + self.suffix]
                rows["attention_mask"] = [[1] * len(rows["input_ids"][0])]
                inputs = self.rerank_tokenizer.pad(
                    rows, padding=True, return_tensors="pt"
                ).to("cuda")
                output = self.rerank_model(
                    **inputs, use_cache=False, logits_to_keep=1
                ).logits[:, -1, :]
                score = torch.softmax(output[:, [self.no, self.yes]].float(), dim=1)[
                    0, 1
                ].item()
                if not math.isfinite(score):
                    raise ValueError("non-finite score")
                scores.append(score)
        return scores


def create_app(engine=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = (
            engine if engine is not None else await run_in_threadpool(QwenEngine)
        )
        app.state.gate = asyncio.Semaphore(1)
        app.state.pending = 0
        yield

    app = FastAPI(
        title="CS local Qwen", lifespan=lifespan, docs_url=None, redoc_url=None
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse({"error": "invalid_model_request"}, status_code=422)

    async def execute(function):
        if app.state.pending >= 8:
            raise HTTPException(503, "model_busy")
        app.state.pending += 1
        acquired = False
        task = None
        try:
            try:
                await asyncio.wait_for(app.state.gate.acquire(), timeout=3)
                acquired = True
            except TimeoutError:
                raise HTTPException(503, "model_busy") from None
            task = asyncio.create_task(run_in_threadpool(function))
            return await asyncio.shield(task)
        except ValueError:
            raise HTTPException(422, "encoding_budget_or_result_invalid") from None
        except RuntimeError:
            raise HTTPException(503, "local_model_unavailable") from None
        finally:
            app.state.pending -= 1
            if acquired:
                # 请求取消不能释放仍在推理的GPU，避免后续请求叠加占用显存。
                if task is not None and not task.done():
                    task.add_done_callback(lambda _: app.state.gate.release())
                else:
                    app.state.gate.release()

    @app.get("/health")
    async def health():
        return {**app.state.engine.health(), "process_id": os.getpid()}

    @app.post("/v1/embeddings")
    async def embeddings(body: EmbeddingRequest):
        if any(not text.strip() or len(text) > 12000 for text in body.input):
            raise HTTPException(422, "invalid_model_request")
        vectors = await execute(
            lambda: app.state.engine.embed(body.input, body.input_type)
        )
        return {
            "object": "list",
            "model": EMBEDDING,
            "data": [
                {"object": "embedding", "index": i, "embedding": vector}
                for i, vector in enumerate(vectors)
            ],
        }

    @app.post("/v1/rerank")
    async def rerank(body: RerankRequest):
        if any(not text.strip() or len(text) > 12000 for text in body.documents):
            raise HTTPException(422, "invalid_model_request")
        scores = await execute(
            lambda: app.state.engine.rerank(body.query, body.documents)
        )
        indexes = sorted(range(len(scores)), key=lambda i: (-scores[i], i))[
            : body.top_n
        ]
        return {
            "model": RERANKER,
            "results": [
                {
                    "index": i,
                    "relevance_score": scores[i],
                    "score_type": "provider_relevance",
                }
                for i in indexes
            ],
        }

    return app


app = create_app()
