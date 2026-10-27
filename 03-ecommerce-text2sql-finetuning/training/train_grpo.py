"""Framework-free PyTorch GRPO, with a frozen SFT reference and real SQL reward.

No training framework, external service, default PostgreSQL, or held-out answer
is used. ``--dry-run`` checks training inputs without loading model or tokenizer.
Checkpoints are saved at completed rollout boundaries so RNG resume is faithful.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_jsonl, prompt
from grpo_math import completion_mask, grpo_loss, normalize_group, token_log_probs


def fingerprint(path):
    path = Path(path)
    digest = hashlib.sha256()
    files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    if not files:
        raise ValueError(f"empty SFT adapter path: {path}")
    for file in files:
        digest.update(str(file.relative_to(path) if path.is_dir() else file.name).encode())
        with file.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def check_train(cfg):
    from config import resolve_path
    if cfg["model"]["torch_dtype"] not in {"bfloat16", "float32"}:
        raise ValueError("native GRPO supports BF16/FP32 only; float16 requires a resumable GradScaler")
    path = resolve_path(cfg["data"]["train"])
    # Never inspect dev/test content in the GRPO path.
    if path in {resolve_path(cfg["data"][key]) for key in ("dev", "test")}:
        raise ValueError("GRPO train path must differ from dev/test")
    rows = load_jsonl(path)
    if not rows or any(r.get("split") != "train" for r in rows):
        raise ValueError("GRPO accepts only nonempty explicitly marked train data")
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("duplicate train ids")
    manifest = json.loads(resolve_path(cfg["data"]["manifest"]).read_text(encoding="utf-8"))
    expected = manifest.get("files", {}).get("train", {}).get("sha256")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if not expected or expected != actual:
        raise ValueError("train file does not match the frozen manifest")
    if cfg["grpo"]["num_generations"] < 2:
        raise ValueError("GRPO needs at least two candidates per group")
    for key in ("max_steps", "batch_size", "gradient_accumulation_steps", "max_prompt_tokens",
                "max_completion_tokens", "updates_per_rollout", "save_every"):
        if cfg["grpo"][key] < 1:
            raise ValueError(f"grpo.{key} must be positive")
    if cfg["grpo"]["temperature"] <= 0 or not 0 < cfg["grpo"]["top_p"] <= 1:
        raise ValueError("invalid sampling configuration")
    if cfg["grpo"]["top_p"] != 1:
        raise ValueError("native GRPO requires top_p=1 and top_k=0 for behavior-policy likelihood alignment")
    if cfg["grpo"]["beta"] < 0 or not 0 < cfg["grpo"]["clip_epsilon"] < 1:
        raise ValueError("invalid GRPO regularization")
    return rows, {"train_rows": len(rows), "train_sha256": actual,
                  "train_only": True, "model_loaded": False,
                  "status": "data_check_only_not_training"}


def collect_rollout(model, tokenizer, row, cfg, oracle, device):
    import torch
    from lora import reference_policy

    opts = cfg["grpo"]
    model.eval()
    ids = tokenizer(prompt(row), add_special_tokens=False, return_tensors="pt")["input_ids"]
    length = ids.shape[-1]
    if length > opts["max_prompt_tokens"]:
        raise ValueError(f"prompt too long; truncation is forbidden: {row['id']} ({length})")
    ids = ids.to(device)
    eos = tokenizer.eos_token_id
    pad = tokenizer.pad_token_id
    if eos is None or pad is None:
        raise ValueError("tokenizer must define EOS and padding token IDs")
    with torch.no_grad():
        generated = model.generate(input_ids=ids, attention_mask=torch.ones_like(ids),
                                   num_return_sequences=opts["num_generations"],
                                   do_sample=True, temperature=opts["temperature"],
                                   top_p=1.0, top_k=0, max_new_tokens=opts["max_completion_tokens"],
                                   eos_token_id=eos, pad_token_id=pad, use_cache=True)
    completed = generated[:, length:]
    masks = completion_mask(completed, eos, pad)
    # Same-length prompts allow one old/ref forward per group. Padding stays
    # masked in attention and is discarded before storing per-completion data.
    attention = torch.cat((torch.ones_like(generated[:, :length]),
                           masks.to(dtype=generated.dtype)), dim=-1)
    with torch.no_grad():
        old_group = token_log_probs(model, generated, attention, length, opts["temperature"])
        with reference_policy(model):
            reference_group = token_log_probs(model, generated, attention, length,
                                               opts["temperature"])
    records, samples = [], []
    for index in range(generated.shape[0]):
        count = int(masks[index].sum().item())
        if count < 1:
            raise RuntimeError("model returned an empty completion")
        full = generated[index:index + 1, :length + count]
        text = tokenizer.decode(full[0, length:], skip_special_tokens=True)
        record = oracle.score(row, text)
        records.append(record)
        samples.append({"ids": full.cpu(), "prompt_length": length,
                        "old": old_group[index:index + 1, :count].cpu(),
                        "reference": reference_group[index:index + 1, :count].cpu(),
                        "mask": masks[index:index + 1, :count].cpu(),
                        "completion_tokens": count})
    advantages = normalize_group([record["reward"] for record in records])
    for sample, advantage in zip(samples, advantages):
        sample["advantage"] = advantage
    return samples, {"id": row["id"], "rewards": [r["reward"] for r in records],
                     "zero_variance": all(a == 0. for a in advantages),
                     "statuses": [r["status"] for r in records]}


def train(cfg, rows, sft_adapter, resume_from, consumer01_root):
    from config import resolve_path
    from output_lock import exclusive_output
    with exclusive_output(resolve_path(cfg['runtime']['output_dir']), name='.training.lock'):
        return _train(cfg, rows, sft_adapter, resume_from, consumer01_root)


def _train(cfg, rows, sft_adapter, resume_from, consumer01_root):
    from config import resolve_path
    from runtime import load_checkpoint, load_policy, save_checkpoint, seed_everything, write_json

    opts = cfg["grpo"]
    output = resolve_path(cfg["runtime"]["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    summary_path = output / "run-summary.json"
    source = resolve_path(sft_adapter)
    source_hash = fingerprint(source)
    start = time.monotonic()
    metrics = {"status": "initializing", "sft_adapter": str(source),
               "sft_sha256": source_hash, "optimizer_steps": 0,
               "parameter_updates": 0, "zero_variance_groups": 0,
               "nonconstant_reward_groups": 0, "learning_signal": False,
               "completion_tokens": 0, "train_only": True,
               "reference": "frozen_sft_adapter_shared_frozen_base"}
    write_json(summary_path, metrics)
    last_saved = 0
    elapsed_offset = 0.0
    try:
        if cfg["model"]["torch_dtype"] not in {"bfloat16", "float32"}:
            raise ValueError("native GRPO supports BF16/FP32 only; float16 requires a resumable GradScaler")
        import torch
        from lora import adapter_parameters, freeze_reference, save_adapter
        from rewards import SQLReward
        seed_everything(opts["seed"])
        with SQLReward(rows, consumer01_root, resolve_path(cfg["data"]["manifest"]),
                       fixtures=cfg['reward']['fixtures']) as oracle:
            model, tokenizer = load_policy(cfg, adapter_path=source, trainable=True)
            from provenance import training_identity
            model._native_training_identity = training_identity(cfg, 'grpo', consumer01_root)
            # Capture reference BEFORE loading any GRPO resume adapter.
            freeze_reference(model)
            model.eval()  # GRPO old/current/ref likelihoods must have no dropout noise.
            pairs = list(adapter_parameters(model))
            if not pairs or any(p.requires_grad for name, p in model.named_parameters()
                                if name not in {n for n, _ in pairs}):
                raise RuntimeError("only native LoRA parameters may be trainable")
            parameters = [parameter for _, parameter in pairs]
            device = next(model.parameters()).device
            optimizer = torch.optim.AdamW(parameters, lr=opts["learning_rate"],
                                          weight_decay=opts.get("weight_decay", cfg["sft"]["weight_decay"]))
            step = 0
            if resume_from:
                state = load_checkpoint(resolve_path(resume_from), model, optimizer)
                extra = state.get("extra", {})
                if extra.get("sft_sha256") != source_hash:
                    raise ValueError("resume SFT reference differs from checkpoint")
                if extra.get("train_sha256") != hashlib.sha256(
                        resolve_path(cfg["data"]["train"]).read_bytes()).hexdigest():
                    raise ValueError("resume training data differs from checkpoint")
                previous = extra.get("config")
                # Output/time budget/max_steps may change, optimization semantics may not.
                if previous is None:
                    raise ValueError("resume checkpoint has no configuration")
                for section in ("model", "adapter", "data"):
                    if previous[section] != cfg[section]:
                        raise ValueError(f"resume configuration changed: {section}")
                for key, value in opts.items():
                    if key not in {"max_steps", "resume_from", "save_every"} and previous["grpo"].get(key) != value:
                        raise ValueError(f"resume GRPO configuration changed: {key}")
                step = int(state["step"])
                metrics.update(extra.get("metrics", {}))
                elapsed_offset = metrics.get("elapsed_seconds", 0.0)
                last_saved = step
            if device.type == "cuda":
                torch.cuda.reset_peak_memory_stats(device)
            train_hash = hashlib.sha256(resolve_path(cfg["data"]["train"]).read_bytes()).hexdigest()

            def checkpoint():
                extra = {"sft_sha256": source_hash, "train_sha256": train_hash,
                         "config": cfg, "metrics": dict(metrics)}
                path = output / f"checkpoint-{step}"
                save_checkpoint(path, model, optimizer, step, extra=extra)
                write_json(output / "latest-checkpoint.json", {"path": str(path), "step": step})

            metrics["status"] = "running"
            while step < opts["max_steps"]:
                budget = cfg["runtime"]["max_seconds"]
                if budget not in (None, 0) and time.monotonic() - start >= budget:
                    metrics["status"] = "time_budget_reached"
                    break
                samples, groups = [], []
                for _ in range(opts["gradient_accumulation_steps"]):
                    for _ in range(opts["batch_size"]):
                        row = random.choice(rows)
                        candidate_samples, group = collect_rollout(model, tokenizer, row, cfg,
                                                                     oracle, device)
                        samples.extend(candidate_samples)
                        groups.append(group)
                metrics["zero_variance_groups"] += sum(g["zero_variance"] for g in groups)
                metrics["nonconstant_reward_groups"] += sum(not g["zero_variance"] for g in groups)
                metrics["learning_signal"] = metrics["nonconstant_reward_groups"] > 0
                metrics["completion_tokens"] += sum(s["completion_tokens"] for s in samples)
                model.train()  # Transformer checkpointing is active in training mode.
                for module in model.modules():
                    if isinstance(module, torch.nn.Dropout):
                        module.eval()  # GRPO likelihood comparisons require no dropout noise.
                for _ in range(min(opts["updates_per_rollout"], opts["max_steps"] - step)):
                    optimizer.zero_grad(set_to_none=True)
                    losses, kls, clipping = [], [], []
                    # Microbatch one completion: limits vocabulary-logit peak memory.
                    # Averaging over all samples equals averaging equal-size groups.
                    for sample in samples:
                        full = sample["ids"].to(device)
                        current = token_log_probs(model, full, torch.ones_like(full),
                                                  sample["prompt_length"], opts["temperature"])
                        loss, stats = grpo_loss(current, sample["old"].to(device),
                                                sample["reference"].to(device),
                                                torch.tensor([sample["advantage"]], device=device),
                                                sample["mask"].to(device), opts["beta"],
                                                opts["clip_epsilon"])
                        if not bool(torch.isfinite(loss)):
                            raise FloatingPointError("nonfinite GRPO loss")
                        (loss / len(samples)).backward()
                        losses.append(loss.detach().item())
                        kls.append(stats["kl"])
                        clipping.append(stats["clip_fraction"])
                    norm = torch.nn.utils.clip_grad_norm_(parameters, opts["max_grad_norm"],
                                                         error_if_nonfinite=True)
                    # Small LoRA tensors only; exact comparison reports real updates.
                    before = [p.detach().clone() for p in parameters]
                    optimizer.step()
                    changed = any(not torch.equal(old, p.detach()) for old, p in zip(before, parameters))
                    del before
                    step += 1
                    metrics["optimizer_steps"] = step
                    metrics["parameter_updates"] += int(changed)
                    elapsed = elapsed_offset + time.monotonic() - start
                    record = {"step": step, "loss": sum(losses) / len(losses),
                              "kl": sum(kls) / len(kls),
                              "clip_fraction": sum(clipping) / len(clipping),
                              "gradient_norm": float(norm), "parameters_changed": changed,
                              "reward_mean": sum(sum(g["rewards"]) for g in groups) / len(samples),
                              "zero_variance_groups": sum(g["zero_variance"] for g in groups),
                              "nonconstant_reward_groups": sum(not g["zero_variance"] for g in groups),
                              "learning_signal": any(not g["zero_variance"] for g in groups),
                              "elapsed_seconds": elapsed, "groups": groups}
                    if device.type == "cuda":
                        metrics["peak_vram_bytes"] = torch.cuda.max_memory_allocated(device)
                    metrics["last_step"] = record
                    metrics["elapsed_seconds"] = elapsed
                    metrics["completion_tokens_per_second"] = metrics["completion_tokens"] / elapsed
                    with (output / "metrics.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    write_json(summary_path, metrics)
                if step - last_saved >= opts["save_every"]:
                    checkpoint()
                    last_saved = step
            if metrics["status"] == "running":
                metrics["status"] = "completed"
            if (step and last_saved != step) or metrics["status"] == "time_budget_reached":
                checkpoint()
            metrics["parameter_update_verified"] = metrics["parameter_updates"] > 0
            metrics["training_verified"] = (metrics["parameter_update_verified"]
                                             and metrics["learning_signal"])
            if metrics["status"] == "time_budget_reached":
                raise RuntimeError("GRPO time budget reached before max_steps; partial checkpoint saved")
            if step:
                save_adapter(model, output / "adapter", extra={"step": step, "config": cfg,
                                                               "sft_sha256": source_hash})
    except BaseException as exc:
        if metrics["status"] != "time_budget_reached":
            metrics["status"] = "failed"
        metrics["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        metrics["elapsed_seconds"] = elapsed_offset + time.monotonic() - start
        write_json(summary_path, metrics)
    return metrics


def main():
    from config import load_config, resolve_path
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--output", help="exact output directory; overrides runtime.output_dir")
    ap.add_argument("--sft-adapter")
    ap.add_argument("--resume")
    ap.add_argument("--consumer01-root")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    if args.output:
        cfg["runtime"]["output_dir"] = args.output
    rows, checks = check_train(cfg)
    if args.dry_run:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return
    adapter = args.sft_adapter or cfg["grpo"].get("sft_checkpoint")
    if not adapter:
        ap.error("--sft-adapter or grpo.sft_checkpoint is required; reference must start at SFT")
    consumer = args.consumer01_root or cfg["reward"]["consumer01_root"]
    print(json.dumps(train(cfg, rows, adapter, args.resume or cfg["grpo"].get("resume_from"),
                           resolve_path(consumer)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
