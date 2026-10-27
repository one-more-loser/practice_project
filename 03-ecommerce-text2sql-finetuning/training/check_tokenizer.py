"""Measure real tokenizer lengths and answer-label integrity.

Run on the GPU host after the tokenizer is available (before training):
  python training/check_tokenizer.py --model Qwen/Qwen3-8B-Base \
    --max-seq-len 1152 --output outputs/tokenizer-check.json

It loads the tokenizer only, not model weights. `--model` may point to a local
tokenizer directory. A different model's tokenizer is a format proxy only; do
not use its numbers as the 8B token length evidence.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training"))
from common import completion_text, load_jsonl, prompt  # noqa: E402
from train_sft import tokenize_example  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3-8B-Base")
    ap.add_argument("--max-seq-len", type=int, default=1152)
    ap.add_argument("--data-dir", default="data/sft_v1")
    ap.add_argument("--output", default="outputs/tokenizer-check.json")
    ap.add_argument("--local-files-only", action="store_true")
    args = ap.parse_args()

    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True,
                                        local_files_only=args.local_files_only)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    data = ROOT / args.data_dir
    rows = []
    for split in ("train", "dev", "test"):
        rows += load_jsonl(data / f"{split}.jsonl")
    p_lens, c_lens, total = [], [], []
    bad = []
    for row in rows:
        p_ids = tok(prompt(row), add_special_tokens=False)["input_ids"]
        c_ids = tok(completion_text(row), add_special_tokens=False)["input_ids"]
        if c_ids and c_ids[-1] != tok.eos_token_id:
            c_ids = c_ids + [tok.eos_token_id]
        p_lens.append(len(p_ids))
        c_lens.append(len(c_ids))
        total.append(len(p_ids) + len(c_ids))
        try:
            ex = tokenize_example(tok, row, args.max_seq_len, allow_truncate=False)
        except ValueError as exc:
            bad.append({"id": row["id"], "error": str(exc)})
            continue
        labels = ex["labels"]
        first = next((i for i, x in enumerate(labels) if x != -100), None)
        if first is None or labels[first:] != c_ids:
            bad.append({"id": row["id"], "error": "label mask does not equal completion ids"})
        if ex["input_ids"][-1] != tok.eos_token_id:
            bad.append({"id": row["id"], "error": "last token is not EOS"})
        decoded = tok.decode(labels[first:] if first is not None else [], skip_special_tokens=False)
        if not decoded.strip().startswith("{"):
            bad.append({"id": row["id"], "error": "decoded labels do not start with JSON"})

    def stats(xs):
        return {"min": min(xs), "max": max(xs), "mean": round(statistics.mean(xs), 2)}

    out = {
        "model_or_tokenizer": args.model,
        "class": type(tok).__name__,
        "vocab_size": tok.vocab_size,
        "eos_token": tok.eos_token,
        "eos_token_id": tok.eos_token_id,
        "pad_token_id": tok.pad_token_id,
        "bos_token_id": tok.bos_token_id,
        "has_chat_template": bool(getattr(tok, "chat_template", None)),
        "max_seq_len": args.max_seq_len,
        "rows": len(rows),
        "prompt_tokens": stats(p_lens),
        "answer_tokens": stats(c_lens),
        "total_tokens": stats(total),
        "over_max_seq_len": sum(1 for x in total if x > args.max_seq_len),
        "label_integrity_failures": bad[:20],
        "label_failure_count": len(bad),
        "note": "tokenizer-only; no model weights downloaded; if --model is not Qwen3-8B-Base tokenizer, numbers are a format proxy",
    }
    outpath = ROOT / args.output
    outpath.parent.mkdir(parents=True, exist_ok=True)
    outpath.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
