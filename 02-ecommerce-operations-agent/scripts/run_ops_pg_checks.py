"""显式获准后运行本run专用02 PG检查；不打印URL/密码，不清库。"""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / ".runtime" / "XOPS-20261023-01"
private = json.loads((RUN / "database-private.json").read_text(encoding="utf-8-sig"))
url = private["url"]
from sqlalchemy.engine import make_url
secret = make_url(url).password
os.environ["OPS_DATABASE_URL"] = url
os.environ["OPS_DATABASE_IDENTITY_FILE"] = str(RUN / "database-identity.json")
os.environ["PYTHONPATH"] = str(ROOT / "backend" / "src")
os.environ["OPS_PG_TEST"] = "1"


def sanitized(value):
    value = value.replace(url, "[REDACTED_URL]")
    return value.replace(secret, "[REDACTED]") if secret else value


def command(name, arguments):
    result = subprocess.run([sys.executable, *arguments], cwd=ROOT / "backend", text=True,
                            capture_output=True, encoding="utf-8", errors="replace")
    output = sanitized(result.stdout + result.stderr)
    (RUN / (name + ".log")).write_text(output, encoding="utf-8")
    print(output)
    if result.returncode:
        raise SystemExit(result.returncode)


command("pg-init-db", ["-m", "ops_agent", "init-db"])
sys.path.insert(0, str(ROOT / "backend" / "src"))
from ops_agent.db import Database, SCHEMA_VERSION
from sqlalchemy import text
database = Database(url, "integration")
database.verify_schema()
with database.engine.connect() as connection:
    role = dict(connection.execute(text("SELECT current_user AS role, rolsuper, rolbypassrls, rolconnlimit FROM pg_roles WHERE rolname=current_user")).mappings().one())
record = {"integration_run_id": "XOPS-20261023-01", "schema_version": SCHEMA_VERSION,
          "identity": database.identity_record, "role": role, "operation": "non-destructive init-db and verify_schema"}
(RUN / "database-initialized.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(record, ensure_ascii=False))
database.close()
command("pg-tests", ["-m", "pytest", "-q", "tests/test_postgres.py", "--tb=short",
                     "--basetemp", str(RUN / "pytest-pg-real"), "--junitxml", str(RUN / "operations-pg-real.xml")])
