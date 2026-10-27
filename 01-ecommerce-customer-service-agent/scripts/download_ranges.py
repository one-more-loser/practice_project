"""公开权重/依赖的可恢复分段下载；严格校验Range、长度和SHA256。"""

import hashlib
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx


def download(url, target: Path, size: int, sha256: str, workers=8):
    if target.exists():
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != sha256:
                raise RuntimeError("existing target checksum mismatch")
        return
    part_dir = target.parent / (target.name + ".parts")
    part_dir.mkdir(parents=True, exist_ok=True)
    step = math.ceil(size / workers)
    seed = target.parent / (target.name + ".partial")
    if seed.exists():
        available = seed.stat().st_size
        if available > size:
            raise RuntimeError("legacy partial exceeds artifact length")
        with seed.open("rb") as source:
            for index in range(workers):
                start = index * step
                count = min(step, max(0, available - start))
                part = part_dir / str(index)
                if count and not part.exists():
                    source.seek(start)
                    with part.open("wb") as output:
                        left = count
                        while left:
                            block = source.read(min(left, 4 * 1024 * 1024))
                            if not block:
                                raise RuntimeError("legacy partial changed during seed")
                            output.write(block)
                            left -= len(block)

    def worker(index):
        start = index * step
        end = min(size, start + step) - 1
        if start > end:
            return None
        part = part_dir / str(index)
        wanted = end - start + 1
        for attempt in range(3):
            present = part.stat().st_size if part.exists() else 0
            if present == wanted:
                return part
            if present > wanted:
                raise RuntimeError("partial range exceeds expected bytes")
            try:
                # 每个范围独立解析公开上游URL，避免复用过期签名或其他范围的缓存。
                with (
                    httpx.Client(follow_redirects=True, timeout=90) as client,
                    client.stream(
                        "GET",
                        url,
                        headers={
                            "Range": f"bytes={start + present}-{end}",
                            "Accept-Encoding": "identity",
                        },
                    ) as response,
                ):
                    expected = f"bytes {start + present}-{end}/{size}"
                    if (
                        response.status_code != 206
                        or response.headers.get("content-range") != expected
                    ):
                        raise RuntimeError("upstream did not honor exact range")
                    with part.open("ab") as stream:
                        for block in response.iter_bytes(1024 * 1024):
                            stream.write(block)
                if part.stat().st_size != wanted:
                    raise RuntimeError("range length mismatch")
                print(
                    f"verified range length {target.name} {index + 1}/{workers}",
                    flush=True,
                )
                return part
            except (httpx.HTTPError, OSError):
                if attempt == 2:
                    raise RuntimeError(
                        "public artifact range download failed"
                    ) from None
                time.sleep(attempt + 1)
        raise RuntimeError("range unavailable")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(worker, range(workers)))
    temporary = target.parent / (target.name + ".verified-pending")
    result = hashlib.sha256()
    with temporary.open("wb") as output:
        for part in parts:
            if part is None:
                continue
            with part.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    output.write(block)
                    result.update(block)
        output.flush()
        os.fsync(output.fileno())
    if temporary.stat().st_size != size or result.hexdigest() != sha256:
        raise RuntimeError("artifact final checksum mismatch")
    temporary.rename(target)
    # 完成后只移除本脚本创建且经过逐段核对的文件；不递归清理共享目录。
    for part in parts:
        if part is not None:
            part.unlink()
    part_dir.rmdir()
    if seed.exists():
        seed.unlink()
    print(f"verified SHA256 {target.name}", flush=True)
