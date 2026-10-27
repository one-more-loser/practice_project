"""INT-20261026-01 only. No legacy profiles, secrets in output, or implicit DB fallback."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = "INT-20261026-01"
PRIVATE = ROOT / ".runtime" / RUN
OUT = ROOT / "docs/07-iteration/ITER-20261025-01/review-output" / ("integration-" + RUN)
HOST, PORT, SERVER_PORT = "192.168.10.100", 55432, 5432
DATABASES = {"cs": "cs_agent_int_20261026_01", "ops": "ops02_int_20261026_01"}
ROLES = {"cs_owner": "csint_20261026_owner", "cs_app": "csint_20261026_app",
         "cs_query": "csint_20261026_query", "ops_owner": "opsint_20261026_owner",
         "ops_app": "opsint_20261026_app"}
COMPOSE = "/home/zzh/ecommerce-customer-service-db/compose.yaml"
ROLE_MAP = {"cs_agent_owner_b0": ROLES["cs_owner"], "cs_agent_app_b0": ROLES["cs_app"],
            "cs_agent_query_b2": ROLES["cs_query"]}
INITIAL_TIME = "2026-10-26T04:00:00+00:00"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def clear_environment():
    for name in list(os.environ):
        if name.startswith(("CS_", "OPS_", "PG")) or name in {"DEEPSEEK_API_KEY", "DATABASE_URL"}:
            os.environ.pop(name)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", PYTHONUTF8="1")


def configuration():
    value = read(PRIVATE / "resources-private.json")
    if (value.get("integration_run_id") != RUN or value.get("databases") != DATABASES
            or value.get("roles") != ROLES):
        raise RuntimeError("run private resource identity mismatch")
    return value


def database_url(key, config=None):
    from sqlalchemy import URL
    value = config or configuration()
    database = DATABASES["cs" if key.startswith("cs_") else "ops"]
    return URL.create("postgresql+psycopg", username=ROLES[key], password=value["passwords"][key],
                      host=HOST, port=PORT, database=database)


def apply_cs(role="cs_app"):
    clear_environment()
    cfg = configuration()
    os.environ.update(
        CS_DATABASE_URL=database_url(role, cfg).render_as_string(hide_password=False),
        CS_QUERY_DATABASE_URL=database_url("cs_query", cfg).render_as_string(hide_password=False),
        CS_EXPECTED_DATABASE=DATABASES["cs"], CS_ISOLATION_FILE=str(PRIVATE / "isolation.json"),
        CS_BROWSER_ORIGIN="http://127.0.0.1:18173", CS_COOKIE_SECURE="false",
        CS_MODEL_SERVICES_FILE=str(PRIVATE / "models-disabled.json"),
        CS_AGENT_WORKER_ENABLED="false", CS_EXPIRATION_WORKER_ENABLED="false",
        CS_ACTIVITY_DELIVERY_WORKER_ENABLED="false", CS_ACTIVITY_OPS_BASE_URL="http://127.0.0.1:18120",
        CS_ACTIVITY_SERVICE_TOKEN=cfg["service_token"], CS_ACTIVITY_SERVICE_STORES=json.dumps(["int_store_a"]),
        CS_ACTIVITY_SNAPSHOT_INPUTS_FILE=str(PRIVATE / "synthetic-inputs.json"),
        CS_INTEGRATION_RUN_ID=RUN,
    )
    return cfg
