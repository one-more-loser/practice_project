"""本轮SSH只读现场核验。密码交互读取，禁止写日志或命令行。"""

import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".runtime/ssh-tools"))
import paramiko


def inspect_remote():
    client = paramiko.SSHClient()
    client.load_host_keys(str(ROOT / ".runtime/ssh/known_hosts"))
    password = getpass.getpass("SSH password (not logged): ")
    try:
        client.connect("192.168.10.100", username="zzh", password=password,
                       look_for_keys=False, allow_agent=False, timeout=5, auth_timeout=5)
        password = None
        command = "cd /home/zzh/ecommerce-customer-service-db && docker compose ps --format json"
        _, output, _error = client.exec_command(command, timeout=10)
        raw = output.read().decode("utf-8")
        if output.channel.recv_exit_status():
            raise RuntimeError("remote read-only inspection failed")
        rows = [json.loads(line) for line in raw.splitlines() if line]
        result = [{key: row.get(key) for key in ("Name", "Project", "Service", "State", "Health", "Image")}
                  for row in rows]
        destination = ROOT / "docs/06-acceptance/XOPS-20261023-01/ssh-container.json"
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    finally:
        client.close()


if __name__ == "__main__":
    inspect_remote()
