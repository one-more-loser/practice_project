"""只停止健康端点与本项目记录均确认的实际Qwen进程，释放GPU。"""

import json
import os
import signal
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / ".runtime/qwen-service.json"
    meta = json.loads(path.read_text(encoding="utf8"))
    with httpx.Client(trust_env=False, timeout=2) as client:
        response = client.get("http://127.0.0.1:8010/health")
        response.raise_for_status()
        health = response.json()
        if (
            health.get("process_id") != meta["pid"]
            or health.get("models") != meta["health"]["models"]
            or health.get("service_revision") != "cs-qwen-local-v1"
        ):
            raise RuntimeError("service owner changed; refusing to stop")
        os.kill(meta["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                client.get("http://127.0.0.1:8010/health")
            except httpx.HTTPError:
                meta["stopped"] = True
                path.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf8",
                )
                print("verified local model service stopped; snapshots preserved")
                return
            time.sleep(0.1)
    raise RuntimeError(
        "service still reachable; inspect exact process before any other action"
    )


if __name__ == "__main__":
    main()
