"""启动短时真实HTTP进程探测；不运行模型，不在证据中保存身份凭据/完整资料。"""

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from cs_agent.runtime_profiles import profile_values

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = {k: v for k, v in os.environ.items() if not k.startswith("CS_")}
    profile, values = profile_values()
    env.update(
        {
            "CS_DATABASE_URL": values["CS_DATABASE_URL"],
            "CS_ISOLATION_FILE": values["CS_ISOLATION_FILE"],
            "CS_EXPECTED_DATABASE": values["CS_EXPECTED_DATABASE"],
            "CS_COOKIE_SECURE": "false",
            "PYTHONUTF8": "1",
        }
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env["CS_BROWSER_ORIGIN"] = base
    evidence = {
        "level": "real_http_and_postgresql",
        "stage": "debian_database" if profile == "debian" else "B1_foundation_regression",
        "database_profile": profile,
        "checks": {},
    }
    accounts = json.loads((ROOT / ".runtime/demo-accounts.json").read_text())
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(path, method="GET", data=None, csrf=None):
        headers = {"Origin": base, "Content-Type": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        req = Request(
            base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers,
            method=method,
        )
        try:
            response = opener.open(req, timeout=5)
        except HTTPError as response:
            return response.code, json.load(response), response.headers
        with response:
            return response.status, json.load(response), response.headers

    with (ROOT / ".runtime/http-server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "cs_agent.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
                "--log-level",
                "warning",
            ],
            env=env,
            cwd=ROOT,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise RuntimeError("HTTP process exited before readiness")
                try:
                    code, body, _ = call("/health/ready")
                    if code == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("HTTP readiness timeout")
                time.sleep(0.2)
            assert body["data"]["database"] == "ready"
            assert body["data"]["chat_model"] == "unvalidated"
            evidence["checks"]["foundation_ready_only"] = True
            code, _, _ = call("/api/v1/me/shipping-inputs/shipping_sim_a")
            assert code == 401
            code, body, headers = call(
                "/api/v1/session",
                "POST",
                {"login_name": "customer_a", "password": accounts["customer_a"]},
            )
            assert code == 200
            assert (
                "HttpOnly" in headers["Set-Cookie"] and "SameSite=strict" in headers["Set-Cookie"]
            )
            csrf = body["data"]["csrf_token"]
            code, body, _ = call("/api/v1/me/shipping-inputs/shipping_sim_a")
            assert code == 200 and body["data"]["phone"].endswith("3456")
            assert call("/api/v1/me/shipping-inputs/shipping_sim_b")[0] == 404
            evidence["checks"]["guest_self_foreign"] = True
            assert call("/api/v1/session", "DELETE")[0] == 403
            assert call("/api/v1/session", "DELETE", csrf=csrf)[0] == 200
            assert call("/api/v1/session")[0] == 401
            evidence["checks"]["csrf_logout_cookie_flags"] = True
        finally:
            process.terminate()
            process.wait(timeout=10)
    evidence["server_process_stopped"] = True
    evidence["model_validation"] = "not_executed"
    output = ROOT / (
        "docs/06-acceptance/debian-db/real-http.json"
        if profile == "debian"
        else "docs/06-acceptance/B1/foundation-http.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
