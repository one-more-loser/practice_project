"""Read-only native-training preflight; never downloads or loads model weights."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKED = {"torch": "2.7.1+cu128", "transformers": "4.51.3", "tokenizers": "0.21.1",
          "huggingface-hub": "0.30.2", "safetensors": "0.5.3", "datasets": "3.5.0",
          "numpy": "2.2.5", "sqlglot": "27.20.0", "psycopg": "3.2.9", "pgserver": "0.1.4"}


def resolve_path(value: str, root: Path = ROOT) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def inspect_model(path: Path) -> dict:
    """Inspect indexed safetensor files, including structural header integrity.

    This detects missing/truncated shards; it is not an upstream checksum check.
    """
    errors = []
    if not path.is_dir():
        return {"ok": False, "errors": [f"model directory missing: {path}"]}
    try:
        cfg = json.loads((path / "config.json").read_text(encoding="utf-8"))
        if cfg.get("model_type") != "qwen3":
            errors.append("config.model_type must be qwen3")
        if cfg.get("hidden_size") != 4096 or cfg.get("num_hidden_layers") != 36:
            errors.append("config does not match Qwen3-8B-Base shape (4096 hidden, 36 layers)")
    except (OSError, ValueError) as exc:
        errors.append(f"config.json: {exc}")
    for name in ("tokenizer.json", "tokenizer_config.json"):
        if not (path / name).is_file() or not (path / name).stat().st_size:
            errors.append(f"missing/empty {name}")
    shards = []
    try:
        index_file = path / "model.safetensors.index.json"
        if index_file.exists():
            index = json.loads(index_file.read_text(encoding="utf-8"))
            shards = sorted(set(index["weight_map"].values()))
            if not shards:
                errors.append("empty weight_map")
        else:
            shards = ["model.safetensors"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"weight index: {exc}")
    total = 0
    for name in shards:
        shard = path / name
        if not shard.resolve().is_relative_to(path.resolve()):
            errors.append(f"shard escapes model directory: {name}")
            continue
        try:
            size = shard.stat().st_size
            with shard.open("rb") as stream:
                header_size = int.from_bytes(stream.read(8), "little")
                if not 2 <= header_size <= min(size - 8, 100_000_000):
                    raise ValueError("invalid safetensors header length")
                header = json.loads(stream.read(header_size))
            offsets = [v["data_offsets"] for k, v in header.items() if k != "__metadata__"]
            if not offsets or max(v[1] for v in offsets) != size - 8 - header_size:
                raise ValueError("safetensors payload size does not match header")
            total += size
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{name}: {exc}")
    # A local path name does not establish provenance. Require upstream hashes separately.
    return {"ok": not errors, "path": str(path), "shards": len(shards),
            "weight_bytes": total, "errors": errors,
            "integrity_scope": "structural_only; upstream hash/provenance not verified"}


def check_environment(config: dict, *, no_model: bool = False, offline: bool = False) -> dict:
    checks = []

    def add(name, passed, detail):
        checks.append({"name": name, "status": "pass" if passed else "required", "detail": detail})

    runtime = config.get("runtime", {})
    add("python", sys.version_info[:2] == (3, 11), sys.version.split()[0] + "; pinned environment uses Python 3.11")
    for package, expected in LOCKED.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = "missing"
        add("package:" + package, actual == expected, {"actual": actual, "expected": expected})
    try:
        import torch
        cuda = torch.cuda.is_available()
        add("cuda", cuda, {"available": cuda, "runtime": torch.version.cuda})
        cuda_version = tuple(int(v) for v in (torch.version.cuda or "0.0").split(".")[:2])
        add("cuda_runtime", cuda_version >= (12, 8),
            {"actual": torch.version.cuda, "minimum": "12.8", "note": "wheel runtime, not nvidia-smi driver ceiling"})
        add("sdpa_api", callable(getattr(torch.nn.functional, "scaled_dot_product_attention", None)),
            "API availability only; no attention kernel executed")
        if cuda:
            device = torch.device(runtime.get("device", "cuda"))
            capability = torch.cuda.get_device_capability(device)
            arch = f"sm_{capability[0]}{capability[1]}"
            arches = torch.cuda.get_arch_list()
            add("gpu_architecture", arch in arches,
                {"gpu": torch.cuda.get_device_name(device), "capability": capability,
                 "compiled_arches": arches, "expected_target": "RTX PRO 6000 Blackwell sm_120"})
            add("bf16", torch.cuda.is_bf16_supported(), "BF16 support queried on current CUDA device")
            free, total = torch.cuda.mem_get_info(device)
            minimum = float(runtime.get("min_vram_gb", 90))
            add("free_vram", free / 2**30 >= minimum,
                {"free_gib": round(free / 2**30, 2), "total_gib": round(total / 2**30, 2), "minimum_gib": minimum})
        else:
            for name in ("gpu_architecture", "bf16", "free_vram"):
                add(name, False, "requires CUDA GPU; not measured")
    except Exception as exc:
        add("torch_runtime", False, f"{type(exc).__name__}: {exc}")
        recorded = {c["name"] for c in checks}
        for name in ("cuda", "cuda_runtime", "gpu_architecture", "bf16", "free_vram", "sdpa_api"):
            if name not in recorded:
                add(name, False, "torch runtime unavailable; not measured")
    target = resolve_path(runtime.get("output_dir", "outputs/native-96gb"))
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        free = shutil.disk_usage(target).free / 2**30
        minimum = float(runtime.get("min_disk_gb", 80))
        add("output_disk", free >= minimum,
            {"existing_parent": str(target), "free_gib": round(free, 2), "minimum_gib": minimum})
    except OSError as exc:
        add("output_disk", False, str(exc))
    if no_model:
        checks.append({"name": "model_files", "status": "skipped", "detail": "--no-model; cannot certify training readiness"})
    else:
        details = inspect_model(resolve_path(config.get("model", {}).get("path", "models/Qwen3-8B-Base")))
        add("model_files", details["ok"], details)
    for name in ("train", "dev", "test", "manifest"):
        value = config.get("data", {}).get(name)
        path = resolve_path(value) if value else None
        add("data:" + name, bool(path and path.is_file()), str(path))
    return {"mode": "read_only_offline" if offline else "read_only", "network_used": False,
            "weights_loaded": False, "cuda_kernels_executed": False,
            "preflight_passed": all(c["status"] != "required" for c in checks),
            "ready_for_training": False,
            "readiness_note": "Preflight is necessary but does not prove CUDA forward/backward, throughput or model provenance.",
            "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "training/configs/rtx_pro_6000_96gb.json"))
    parser.add_argument("--offline", action="store_true", help="no network (also the default behavior)")
    parser.add_argument("--no-model", action="store_true", help="report model check as skipped")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        report = check_environment(config, no_model=args.no_model, offline=args.offline)
    except (OSError, ValueError) as exc:
        report = {"preflight_passed": False, "ready_for_training": False, "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["preflight_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
