"""启动本项目回环Qwen服务；不抢端口，失败只终止自身子进程。"""

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    python = ROOT / ".runtime/qwen-venv/Scripts/python.exe"
    if not python.is_file() or not (ROOT / ".runtime/qwen-models.json").is_file():
        raise RuntimeError("verified model snapshots and isolated environment required")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8010)) == 0:
            raise RuntimeError(
                "local model port occupied; inspect existing owner, do not terminate"
            )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("CS_")
        and not key.upper().endswith(("API_KEY", "API-KEY"))
    }
    env.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
        HF_HOME=str(ROOT / ".runtime/hf-service"),
        TORCH_HOME=str(ROOT / ".runtime/torch-cache"),
        PYTHONUTF8="1",
    )
    log_path = ROOT / ".runtime/qwen-service.log"
    with log_path.open("wb") as log:
        child = subprocess.Popen(
            [
                str(python),
                "-m",
                "uvicorn",
                "model_service.qwen_service:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8010",
                "--no-access-log",
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(
                    "local model startup failed; inspect project private log"
                )
            try:
                response = httpx.get(
                    "http://127.0.0.1:8010/health", timeout=1, trust_env=False
                )
                if (
                    response.status_code == 200
                    and response.json().get("real_model") is True
                ):
                    report = {
                        "pid": response.json()["process_id"],
                        "launcher_pid": child.pid,
                        "host": "127.0.0.1",
                        "port": 8010,
                        "health": response.json(),
                    }
                    (ROOT / ".runtime/qwen-service.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf8",
                    )
                    print(json.dumps(report, ensure_ascii=False))
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise RuntimeError("local model startup deadline exceeded")
    except BaseException:
        if child.poll() is None:
            if os.name == "nt":
                # Windows venv redirector另有实际Python子进程，终止自身树以免遗留GPU服务。
                subprocess.run(
                    ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                child.terminate()
            child.wait(timeout=10)
        raise


if __name__ == "__main__":
    main()
