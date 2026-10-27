"""下载已批准的两种Qwen模型到用户指定共享目录；固定提交并校验权重。"""

import hashlib
import json
import time
from pathlib import Path

import httpx
from download_ranges import download as download_ranges

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT.parent / "shared-models"
REPOS = {
    "embedding": "Qwen/Qwen3-Embedding-0.6B",
    "rerank": "Qwen/Qwen3-Reranker-0.6B",
}


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def main():
    SHARED.mkdir(exist_ok=True)
    output = ROOT / ".runtime/qwen-models.json"
    pinned = (
        json.loads(output.read_text(encoding="utf8"))["models"]
        if output.exists()
        else {}
    )
    result = {"shared_root": str(SHARED.resolve()), "models": {}}
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for capability, repo in REPOS.items():
            saved = pinned.get(capability)
            if saved and saved.get("repository") != repo:
                raise RuntimeError(
                    "existing model repository differs; preserving reference"
                )
            saved_revision = saved.get("revision") if saved else None
            if saved and (
                not isinstance(saved_revision, str)
                or len(saved_revision) != 40
                or any(c not in "0123456789abcdef" for c in saved_revision)
            ):
                raise RuntimeError("invalid pinned revision; preserving reference")
            revision_path = f"/revision/{saved_revision}" if saved_revision else ""
            response = client.get(
                f"https://huggingface.co/api/models/{repo}{revision_path}?blobs=true"
            )
            response.raise_for_status()
            metadata = response.json()
            revision = metadata["sha"]
            if saved_revision and revision != saved_revision:
                raise RuntimeError("upstream did not return the pinned revision")
            if len(revision) != 40 or any(
                c not in "0123456789abcdef" for c in revision
            ):
                raise RuntimeError("invalid upstream revision")
            folder = SHARED / repo / revision
            folder.mkdir(parents=True, exist_ok=True)
            if not folder.resolve().is_relative_to(SHARED.resolve()):
                raise RuntimeError("model directory outside shared root")
            records = []
            for item in metadata["siblings"]:
                name = item["rfilename"]
                if "\\" in name or Path(name).is_absolute() or ".." in Path(name).parts:
                    raise RuntimeError("unsafe upstream file path")
                if not (
                    name.endswith((".json", ".safetensors", ".txt", ".model"))
                    or name in {"README.md", "LICENSE", "merges.txt", "vocab.json"}
                ):
                    continue
                if name.endswith(".safetensors") and name != "model.safetensors":
                    raise RuntimeError("unexpected upstream weight layout")
                target = folder / name
                if not target.resolve().is_relative_to(folder.resolve()):
                    raise RuntimeError("upstream file outside snapshot")
                target.parent.mkdir(parents=True, exist_ok=True)
                expected = item.get("lfs", {}).get("sha256")
                expected_size = item.get("lfs", {}).get("size", item.get("size"))
                if target.exists():
                    if expected and digest(target) != expected:
                        raise RuntimeError(
                            "existing model checksum mismatch; preserving file"
                        )
                    if (
                        expected_size is not None
                        and target.stat().st_size != expected_size
                    ):
                        raise RuntimeError(
                            "existing model size mismatch; preserving file"
                        )
                elif expected and expected_size and name.endswith(".safetensors"):
                    download_ranges(
                        f"https://huggingface.co/{repo}/resolve/{revision}/{name}",
                        target,
                        expected_size,
                        expected,
                    )
                else:
                    partial = folder / (name + ".partial")
                    # 失败保留partial；重试只覆盖该专用临时文件，不覆盖已完成权重。
                    with client.stream(
                        "GET",
                        f"https://huggingface.co/{repo}/resolve/{revision}/{name}",
                    ) as download:
                        download.raise_for_status()
                        saved = 0
                        tick = time.monotonic()
                        with partial.open("wb") as stream:
                            for block in download.iter_bytes(1024 * 1024):
                                stream.write(block)
                                saved += len(block)
                                if time.monotonic() - tick > 20:
                                    print(
                                        f"{capability}/{name}: {saved // 1048576} MiB",
                                        flush=True,
                                    )
                                    tick = time.monotonic()
                    if (
                        expected_size is not None
                        and partial.stat().st_size != expected_size
                    ):
                        raise RuntimeError("download size mismatch")
                    if expected and digest(partial) != expected:
                        raise RuntimeError("download checksum mismatch")
                    partial.rename(target)
                records.append(
                    {
                        "file": name,
                        "bytes": target.stat().st_size,
                        "sha256": digest(target),
                    }
                )
            if "model.safetensors" not in {row["file"] for row in records}:
                raise RuntimeError("verified model weights missing")
            manifest = {"repository": repo, "revision": revision, "files": records}
            marker = folder / "verified-model.json"
            marker.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf8")
            result["models"][capability] = {
                "repository": repo,
                "revision": revision,
                "path": str(folder.resolve()),
                "manifest_sha256": digest(marker),
            }
            print(f"verified {repo}@{revision}: {len(records)} files", flush=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    print("both model snapshots verified; project reference written", flush=True)


if __name__ == "__main__":
    main()
