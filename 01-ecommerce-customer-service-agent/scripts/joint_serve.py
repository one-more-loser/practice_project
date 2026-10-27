"""仅本机联合开发入口，固定test与禁模型；不执行seed/reset/迁移。"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from joint_files import source_manifest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT.parent / "02-ecommerce-operations-agent"
RUN = "XOPS-20261023-01"
PRIVATE = ROOT / ".runtime" / RUN


class JointClock:
    """仅本机隔离开发入口使用的可信文件时钟；无HTTP改时接口。"""
    def now(self):
        raw = json.loads((PRIVATE / "joint-clock.json").read_text(encoding="utf-8"))
        result = datetime.fromisoformat(raw["now"])
        if result.tzinfo is None:
            raise RuntimeError("joint clock must have timezone")
        return result


def main(component):
    configuration = json.loads((PRIVATE / "joint-config.json").read_text(encoding="utf-8"))
    import uvicorn
    if component == "cs":
        from cs_agent.runtime_profiles import apply_runtime_profile
        _, values = apply_runtime_profile("debian")
        os.environ.update(CS_DATABASE_URL=values["CS_TEST_DATABASE_URL"], CS_QUERY_DATABASE_URL=values["CS_TEST_QUERY_DATABASE_URL"],
                          CS_EXPECTED_DATABASE="cs_agent_b0_test", CS_COOKIE_SECURE="false", CS_BROWSER_ORIGIN="http://127.0.0.1:5173",
                          CS_MODEL_SERVICES_FILE=str(PRIVATE / "models-disabled.json"), CS_AGENT_WORKER_ENABLED="false",
                          CS_EXPIRATION_WORKER_ENABLED="true", CS_ACTIVITY_DELIVERY_WORKER_ENABLED="true",
                          CS_ACTIVITY_OPS_BASE_URL=configuration.get("ops_base_url", "http://127.0.0.1:8120"), CS_ACTIVITY_SERVICE_TOKEN=configuration["service_token"],
                          CS_ACTIVITY_SERVICE_STORES=json.dumps(configuration["stores"]), CS_INTEGRATION_RUN_ID=RUN,
                          CS_LOADED_SOURCE_DIGEST=source_manifest(ROOT)["digest"],
                          CS_ACTIVITY_SNAPSHOT_INPUTS_FILE=str(PRIVATE / "synthetic-inputs.json"))
        from cs_agent.main import create_app
        app = create_app(clock=JointClock())
        port = 8000
    elif component == "ops":
        sys.path.insert(0, str(OPS / "backend/src"))
        private = OPS / ".runtime" / RUN
        database = json.loads((private / "database-private.json").read_text(encoding="utf-8"))
        os.environ["OPS_DATABASE_IDENTITY_FILE"] = str(private / "database-identity.json")
        from ops_agent.app import create_app
        app = create_app(database_url=database["url"], mode="integration", origin="http://127.0.0.1:5173", clock=JointClock().now,
                         identity_base_url="http://127.0.0.1:8000", trade_base_url="http://127.0.0.1:8000",
                         service_token=configuration["service_token"], service_stores=tuple(configuration["stores"]))
        port = 8120
    else:
        raise ValueError("unknown joint component")
    loaded = source_manifest(ROOT if component == "cs" else OPS)["digest"]

    @app.get("/health/joint")
    def joint_version():
        return {"integration_run_id": RUN, "service": component, "mode": "integration_synthetic",
                "loaded_source_digest": loaded, "pid": os.getpid(), "parent_pid": os.getppid(),
                "models": "disabled", "origin": "http://127.0.0.1:5173"}

    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main(sys.argv[1])
