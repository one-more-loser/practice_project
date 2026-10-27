"""Loopback-only API entry for the guarded INT run, with all autonomous workers disabled."""
import hashlib
import json
import os
import sys
from datetime import datetime

from int20261026_common import (
    INITIAL_TIME,
    OUT,
    PRIVATE,
    ROOT,
    RUN,
    apply_cs,
    read,
    write,
)
from joint_files import source_manifest

sys.path.insert(0, str(ROOT / "backend/src"))


class FileClock:
    def __init__(self, path=PRIVATE / "clock.json"):
        self.path = path
        self.last = datetime.fromisoformat(INITIAL_TIME)

    def now(self):
        value = read(self.path)
        current = datetime.fromisoformat(value["now"])
        if value.get("integration_run_id") != RUN or current.tzinfo is None or current < self.last:
            raise RuntimeError("run clock missing timezone, wrong identity or moving backward")
        self.last = current
        return current


def main():
    apply_cs()
    loaded = source_manifest(ROOT)["digest"]
    os.environ["CS_LOADED_SOURCE_DIGEST"] = loaded
    import uvicorn
    from cs_agent.config import Settings
    from cs_agent.main import create_app
    settings = Settings()
    assert not any((settings.agent_worker_enabled, settings.expiration_worker_enabled,
                    settings.activity_delivery_worker_enabled))
    app = create_app(settings=settings, clock=FileClock())
    if app.state.model_gateway.configs:
        raise RuntimeError("models must be disabled for this run")
    public_config = {"integration_run_id": RUN, "service": "cs01", "host": "127.0.0.1", "port": 18080,
        "database": settings.expected_database, "identity": read(PRIVATE / "isolation.json"),
        "origin": settings.browser_origin, "cookie_secure": settings.cookie_secure,
        "models": "disabled", "workers": {"agent": False, "expiration": False, "activity_delivery": False},
        "ops_base_url": settings.activity_ops_base_url, "stores": settings.activity_service_stores,
        "model_file_sha256": hashlib.sha256(settings.model_services_file.read_bytes()).hexdigest(),
        "snapshot_inputs_sha256": hashlib.sha256(settings.activity_snapshot_inputs_file.read_bytes()).hexdigest(),
        "clock_file": str(PRIVATE / "clock.json"), "source_digest": loaded,
        "entry_sha256": hashlib.sha256((ROOT / "scripts/int20261026_serve.py").read_bytes()).hexdigest()}
    config_digest = hashlib.sha256(json.dumps(public_config, sort_keys=True).encode()).hexdigest()
    public_config["config_digest"] = config_digest
    write(OUT / "evidence/service-effective-config.json", public_config)

    @app.get("/health/integration")
    def health():
        from fastapi.responses import JSONResponse
        body = {"integration_run_id": RUN, "service": "cs01", "pid": os.getpid(),
                "loaded_source_digest": loaded, "config_digest": config_digest,
                "database_identity": {k: public_config["identity"][k] for k in
                                      ("host", "port", "server_port", "instance_id", "app_role")},
                "database": settings.expected_database, "ready": app.state.db is not None,
                "models": "disabled", "model_capabilities": sorted(app.state.model_gateway.configs),
                "workers": public_config["workers"], "clock": app.state.clock.now().isoformat()}
        return JSONResponse(body, status_code=200 if body["ready"] else 503)

    uvicorn.run(app, host="127.0.0.1", port=18080, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
