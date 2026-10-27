"""Real HTTP/PG smoke on new run resources; redact credentials/cookies/CSRF and retain evidence."""
import json
import sys
import time
from datetime import datetime, timedelta

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

sys.path.insert(0, str(ROOT / "backend/src"))


def main():
    import httpx
    from cs_agent.config import Settings
    from cs_agent.core.context import Role, TrustedContext
    from cs_agent.db.session import verify_target
    from cs_agent.query.executor import SQLExecutor
    from int20261026_prepare import engine_for
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError
    cfg = apply_cs()
    passwords = read(PRIVATE / "accounts-private.json")
    checks = []

    def request(client, method, path, expected, **kwargs):
        started = time.perf_counter()
        response = client.request(method, path, **kwargs)
        data = response.json()
        row = {"method": method, "path": path, "expected_status": expected, "actual_status": response.status_code,
               "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
               "request_id": data.get("request_id"), "error_code": data.get("error", {}).get("code")}
        checks.append(row)
        write(OUT / "evidence/http-smoke.json", {"run": RUN, "requests": checks, "complete": False})
        assert response.status_code == expected, "unexpected status; no headers/credential body in public evidence"
        return data

    base = {"base_url": "http://127.0.0.1:18080", "headers": {"origin": "http://127.0.0.1:18173"},
            "timeout": 8, "trust_env": False}
    with httpx.Client(**base) as client:
        health = request(client, "GET", "/health/integration", 200)
        assert health["integration_run_id"] == RUN and health["models"] == "disabled" and not health["model_capabilities"]
        assert not any(health["workers"].values())
        request(client, "GET", "/health/ready", 200)
        request(client, "GET", "/api/v1/session", 401)
        snapshot_path = "/api/v1/internal/stores/int_store_a/operations-snapshot"
        request(client, "GET", snapshot_path, 401)
        envelope = request(client, "GET", snapshot_path, 200, headers={"authorization": "Bearer " + cfg["service_token"]})
        snapshot = envelope["data"]
        write(OUT / "evidence/snapshot-response.json", envelope)
        assert len(snapshot["skus"]) == 2 and len(snapshot["orders"]) == 5
        assert "int_order_after" not in {o["order_id"] for o in snapshot["orders"]}
        assert next(o for o in snapshot["orders"] if o["order_id"] == "int_order_zero")["status"] == "unpaid"
        now = datetime.fromisoformat(INITIAL_TIME)
        eligible = [o for o in snapshot["orders"] if o["status"] == "paid" and o["paid_at"]
                    and now - timedelta(days=1) <= datetime.fromisoformat(o["paid_at"]) < now]
        assert {o["order_id"] for o in eligible} == {"int_order_start", "int_order_repeat"}
        assert sum(int(o["paid_minor"]) for o in eligible) == 7000
        assert sum(line["qty"] for o in eligible for line in o["items"]) == 6
        again = request(client, "GET", snapshot_path, 200, headers={"authorization": "Bearer " + cfg["service_token"]})["data"]
        assert again == snapshot
        request(client, "GET", "/api/v1/internal/stores/int_store_b/operations-snapshot", 404,
                headers={"authorization": "Bearer " + cfg["service_token"]})
    identities = []
    for name in passwords:
        with httpx.Client(**base) as client:
            session = request(client, "POST", "/api/v1/session", 200, json={"login_name": name, "password": passwords[name]})["data"]
            current = request(client, "GET", "/api/v1/session", 200)["data"]
            assert current["subject_id"] == name and current["store_id"] == session["store_id"]
            identities.append({k: current[k] for k in ("subject_id", "store_id", "active_role", "capabilities", "capability_revision", "auth_version")})
            if name == "int_customer_a":
                request(client, "GET", "/api/v1/me/orders/int_order_repeat", 200)
                request(client, "GET", "/api/v1/me/orders/int_order_b", 404)
            if name == "int_admin_a":
                request(client, "GET", "/api/v1/shop/orders/int_order_b", 404)
    # Proxy actually reaches 01; 02 endpoints remain deliberately unstarted until coordinator's next stage.
    with httpx.Client(base_url="http://127.0.0.1:18173", timeout=8, trust_env=False) as client:
        request(client, "GET", "/health/integration", 200)
        request(client, "GET", "/api/v1/session", 401)
        assert client.get("/").status_code == 200
    write(OUT / "evidence/identity-projections.json", identities)
    query = engine_for("cs_query")
    try:
        verify_target(query, Settings(), query=True)
        with query.connect() as conn:
            assert not conn.execute(text("SELECT has_schema_privilege(current_user,'public','CREATE')")).scalar_one()
            assert not conn.execute(text("SELECT has_table_privilege(current_user,'customer_order','INSERT,UPDATE,DELETE')")).scalar_one()
            assert conn.execute(text("SELECT count(*) FROM csq.cs_order_self")).scalar_one() == 0
        observed = []
        for owner, store, expected in [("int_customer_a", "int_store_a", 6),
                                       ("int_customer_a", "int_store_b", 0), ("int_customer_b", "int_store_b", 1)]:
            with query.connect() as conn, conn.begin():
                ctx = TrustedContext(owner, store, Role.CUSTOMER, "synthetic_scope_check", 1, "int_20261026_01", 1, "run_smoke")
                SQLExecutor.context(conn, ctx, now, {"start_at": now - timedelta(days=10), "end_at": now})
                ids = list(conn.execute(text("SELECT order_id FROM csq.cs_order_self ORDER BY order_id")).scalars())
                assert len(ids) == expected
                observed.append({"owner": owner, "store": store, "orders": ids, "expected_count": expected})
        with query.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM csq.cs_order_self")).scalar_one() == 0
        try:
            with query.connect() as conn:
                conn.execute(text("SELECT password_digest FROM subject LIMIT 0"))
        except ProgrammingError as exc:
            assert exc.orig.sqlstate == "42501"
        else:
            raise AssertionError("query role has unexpected private identity table access")
        write(OUT / "evidence/query-role-smoke.json", {"guards": "passed", "no_context_rows": 0,
              "contexts": observed, "connection_reuse_context_leak": False, "identity_table_denied": "42501",
              "write_privileges": False, "note": "raw restricted-role PG reads; not model Text2SQL acceptance"})
    finally:
        query.dispose()
    write(OUT / "evidence/http-smoke.json", {"integration_run_id": RUN, "complete": True,
          "requests": checks, "unexpected_statuses": 0, "request_count": len(checks), "models_used": 0,
          "snapshot_check": {"paid_orders": 2, "paid_minor": "7000", "units": 6, "half_open": True},
          "limitations": "new synthetic DB only; no 02 calls, activities, worker delivery, faults or clock advances"})
    print(json.dumps({"smoke": "passed", "http_requests": len(checks), "unexpected_statuses": 0, "identities": len(identities), "query_role_contexts": len(observed)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - keep raw credential-bearing response/SQL diagnostics private
        import traceback
        (PRIVATE / "smoke-private-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        print("Failed: " + type(exc).__name__ + "; private diagnostic retained", file=sys.stderr)
        raise SystemExit(1) from None
