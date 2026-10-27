"""Start/status/stop only registered API/Vite processes of INT-20261026-01; never DB containers."""
import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from int20261026_common import OUT, PRIVATE, ROOT, RUN, read, write
from joint_control import process, same_process

REGISTRY = PRIVATE / "processes.json"
RUNTIME_NODE = Path("C:/Users/27997/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe")


def get(url):
    with urllib.request.urlopen(url, timeout=2) as response:
        raw = response.read()
        return json.loads(raw) if "json" in response.headers.get("content-type", "") else {"status": response.status}


def listeners(port):
    command = (f"Get-NetTCPConnection -State Listen -LocalPort {int(port)} -ErrorAction SilentlyContinue "
               "| Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True,
        text=True, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
    rows = json.loads(result.stdout) if result.stdout.strip() else []
    return rows if isinstance(rows, list) else [rows]


def vite_entry():
    path = PRIVATE / "vite-run.mjs"
    body = '''import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const root=ROOT_LITERAL;
const require=createRequire(root+'/frontend/package.json');
const {createServer}=await import(pathToFileURL(require.resolve('vite')).href);
const vue=(await import(pathToFileURL(require.resolve('@vitejs/plugin-vue')).href)).default;
const server=await createServer({configFile:false,root:root+'/frontend',plugins:[vue()],server:{host:'127.0.0.1',port:18173,strictPort:true,proxy:{
 '/api/ops/v1':{target:'http://127.0.0.1:18120'},
 '/api/v1':{target:'http://127.0.0.1:18080',ws:true},
 '/operations':{target:'http://127.0.0.1:18174',ws:true},
 '/health':{target:'http://127.0.0.1:18080'}
}}});
await server.listen();
console.log('INT-20261026-01 loopback 18173 ready');
'''.replace("ROOT_LITERAL", json.dumps(ROOT.as_posix()))
    if path.exists() and path.read_text(encoding="utf-8") != body:
        raise RuntimeError("Vite entry changed; do not overwrite a prior run entry")
    path.write_text(body, encoding="utf-8")
    write(OUT / "evidence/vite-config.json", {"integration_run_id": RUN, "entry": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "host": "127.0.0.1", "port": 18173,
        "configFile": False, "proxy": {"/api/v1": 18080, "/api/ops/v1": 18120, "/operations": 18174}})
    return path


def start():
    if REGISTRY.exists():
        raise RuntimeError("run registry exists; inspect status, do not create duplicate services")
    for port in (18080, 18173):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    entry = vite_entry()
    state = {"integration_run_id": RUN, "created_at": datetime.now(UTC).isoformat(), "processes": []}
    for service, executable, args, port, path in [
        ("cs01", ROOT / "backend/.venv/Scripts/python.exe", ["-B", str(ROOT / "scripts/int20261026_serve.py")], 18080, "/health/integration"),
        ("cs01-web", RUNTIME_NODE, [str(entry)], 18173, "/"),
    ]:
        with (PRIVATE / (service + ".stdout.log")).open("ab") as stdout, (PRIVATE / (service + ".stderr.log")).open("ab") as stderr:
            environment = {k: v for k, v in os.environ.items()
                           if not k.startswith(("CS_", "OPS_", "PG")) and k != "DEEPSEEK_API_KEY"}
            environment.update(PYTHONUTF8="1", PYTHON_DOTENV_DISABLED="1")
            child = subprocess.Popen([str(executable), *args], cwd=ROOT, env=environment,
                stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        row = {"service": service, "launcher_pid": child.pid, "launcher_identity": process(child.pid),
               "pid": child.pid, "port": port, "started_at": datetime.now(UTC).isoformat(), "health": None}
        row["identity"] = row["launcher_identity"]
        state["processes"].append(row)
        write(REGISTRY, state)  # preserve partial launch ownership before health wait
        for _ in range(40):
            try:
                health = get(f"http://127.0.0.1:{port}" + path)
                if service == "cs01" and (health.get("integration_run_id") != RUN or not health.get("ready")):
                    raise RuntimeError("foreign or unready service")
                row.update(pid=health.get("pid", child.pid), health=health)
                break
            except (OSError, ValueError):
                if child.poll() is not None:
                    break
                time.sleep(.25)
        row["identity"] = process(row["pid"])
        write(REGISTRY, state)
        if not row["health"] or not row["identity"]:
            raise RuntimeError("owned service startup failed; private logs/registry preserved")
    status()


def status():
    state = read(REGISTRY)
    if state.get("integration_run_id") != RUN:
        raise RuntimeError("registry run mismatch")
    rows = []
    for row in state["processes"]:
        current = process(row["pid"])
        if not same_process(current, row["identity"]):
            raise RuntimeError("registered process identity changed")
        bound = listeners(row["port"])
        if not bound or any(r["LocalAddress"] != "127.0.0.1" or r["OwningProcess"] != row["pid"] for r in bound):
            raise RuntimeError("listener is missing, public or belongs to another PID")
        url = f"http://127.0.0.1:{row['port']}" + ("/health/integration" if row["service"] == "cs01" else "/")
        rows.append({**row, "current_health": get(url), "listeners": bound, "identity_matches": True})
    write(OUT / "evidence/processes.json", {"integration_run_id": RUN, "processes": rows})
    print(json.dumps({"processes": [{"service": r["service"], "pid": r["pid"], "port": r["port"], "identity_matches": True} for r in rows]}))


def stop():
    state = read(REGISTRY)
    if state.get("integration_run_id") != RUN:
        raise RuntimeError("registry run mismatch")
    for row in reversed(state["processes"]):
        if any(item["OwningProcess"] != row["pid"] for item in listeners(row["port"])):
            raise RuntimeError("port owner changed; do not stop unknown process")
        for pid, expected in [(row["pid"], row["identity"]), (row["launcher_pid"], row["launcher_identity"])]:
            current = process(pid)
            if current is None:
                continue
            if not same_process(current, expected):
                raise RuntimeError("PID reused; refuse stopping unknown process")
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    write(PRIVATE / "stopped.json", {"integration_run_id": RUN, "at": datetime.now(UTC).isoformat()})
    print("owned process stop requested; databases and Docker preserved; verify listeners before any restart")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["start", "status", "stop", "prepare-vite"])
    args = parser.parse_args()
    {"start": start, "status": status, "stop": stop, "prepare-vite": vite_entry}[args.phase]()
