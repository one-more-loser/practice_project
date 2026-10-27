"""归属明确的联合启停，登记实际PID/创建时刻/命令/加载指纹，保留所有数据。"""
import argparse
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT.parent / "02-ecommerce-operations-agent"
RUN = "XOPS-20261023-01"
PRIVATE = ROOT / ".runtime" / RUN
REGISTRY = PRIVATE / "joint-processes.json"


def process(pid):
    command = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); " + f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine,ExecutablePath | ConvertTo-Json -Compress"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, text=True,
                            encoding="utf-8-sig", check=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
    return json.loads(result.stdout) if result.stdout.strip() else None


def read_health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/joint", timeout=1) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def start(only=None):
    previous = json.loads(REGISTRY.read_text(encoding="utf-8")) if REGISTRY.exists() else {"processes": []}
    selected = {only} if only else {"cs", "ops", "cs-web", "ops-web"}
    if any(process(row["pid"]) for row in previous["processes"] if row["service"] in selected):
        raise RuntimeError("registered services exist; use status or stop")
    port_map = {"cs": 8000, "ops": 8120, "cs-web": 5173, "ops-web": 5174}
    for port in (port_map[name] for name in selected):
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))  # 占用端口保留，不结束未知进程。
    config = PRIVATE / "joint-config.json"
    if not config.exists():
        config.write_text(json.dumps({"service_token": secrets.token_urlsafe(48), "stores": ["store_sim"]}), encoding="utf-8")
    (PRIVATE / "models-disabled.json").write_text("{}", encoding="utf-8")
    node = Path(r"C:/Users/27997/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe")
    specifications = [
        ("cs", ROOT, ROOT / "backend/.venv/Scripts/python.exe", [str(ROOT / "scripts/joint_serve.py"), "cs"], 8000),
        ("ops", OPS, OPS / ".venv/Scripts/python.exe", [str(ROOT / "scripts/joint_serve.py"), "ops"], 8120),
        ("ops-web", OPS, node, [str(OPS / "frontend/node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", "5174", "--strictPort"], 5174),
        ("cs-web", ROOT, node, [str(ROOT / "frontend/node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", "5173", "--strictPort"], 5173),
    ]
    state = {"integration_run_id": RUN, "started_at": datetime.now(UTC).isoformat(),
             "processes": [row for row in previous["processes"] if row["service"] not in selected]}
    for name, project, executable, arguments, port in specifications:
        if name not in selected:
            continue
        stdout, stderr = PRIVATE / (name + ".stdout.log"), PRIVATE / (name + ".stderr.log")
        cwd = project / "frontend" if name.endswith("web") else project
        with stdout.open("ab") as out, stderr.open("ab") as err:
            child = subprocess.Popen([str(executable), *arguments], cwd=cwd, stdout=out, stderr=err,
                                      env={**os.environ, "PYTHONUTF8": "1"},
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        actual_pid = child.pid
        health = None
        if not name.endswith("web"):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                health = read_health(port)
                if health and health.get("integration_run_id") == RUN and health.get("service") == name:
                    actual_pid = health["pid"]
                    break
                if child.poll() is not None:
                    break
                time.sleep(0.2)
        else:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                        if response.status == 200:
                            health = {"frontend_http_status": 200}
                            break
                except OSError:
                    pass
                if child.poll() is not None:
                    break
                time.sleep(.2)
        identity = process(actual_pid)
        row = {"service": name, "project": str(project), "mode": "integration_synthetic", "pid": actual_pid,
               "launcher_pid": child.pid, "launcher_identity": process(child.pid), "identity": identity,
               "port": port, "health": health, "stdout": str(stdout), "stderr": str(stderr)}
        state["processes"].append(row)
        REGISTRY.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        if identity is None or health is None:
            raise RuntimeError(name + " failed to start; owned partial registry preserved")
    status()


def same_process(current, expected):
    return bool(current and expected and current["CreationDate"] == expected["CreationDate"]
                and current["ExecutablePath"] == expected["ExecutablePath"]
                and current["CommandLine"] == expected["CommandLine"])


def status():
    state = json.loads(REGISTRY.read_text(encoding="utf-8"))
    public = []
    for row in state["processes"]:
        current = process(row["pid"])
        public.append({"service": row["service"], "pid": row["pid"], "port": row["port"],
                       "identity_matches": same_process(current, row["identity"]),
                       "loaded": read_health(row["port"]) if not row["service"].endswith("web") else None})
    print(json.dumps(public, ensure_ascii=False))


def stop(only=None):
    state = json.loads(REGISTRY.read_text(encoding="utf-8"))
    for row in reversed(state["processes"]):
        if only and row["service"] != only:
            continue
        for pid, expected in [(row["pid"], row["identity"]), (row["launcher_pid"], row["launcher_identity"])]:
            current = process(pid)
            if not current:
                continue
            if not same_process(current, expected):
                raise RuntimeError("PID identity changed; preserved foreign process")
            subprocess.run(["powershell.exe", "-NoProfile", "-Command", f"Stop-Process -Id {int(pid)} -ErrorAction Stop"],
                            check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print("owned joint services stopped; all databases/configuration/data retained")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "status", "stop"])
    parser.add_argument("--only", choices=["cs", "ops", "cs-web", "ops-web"])
    options = parser.parse_args()
    if options.action == "status":
        status()
    else:
        {"start": start, "stop": stop}[options.action](options.only)
