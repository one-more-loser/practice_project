"""固定测试库真实DeepSeek取消审核与原付款退款；不操作应用余额。"""

import json
import sys

from cs_agent.cancellation import CancellationWorker
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.core.clock import Clock
from cs_agent.main import create_app
from cs_agent.runtime_profiles import apply_runtime_profile
from fastapi.testclient import TestClient


def main():
    _, values = apply_runtime_profile("debian")
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from tests.api.test_checkout import create_order, key, propose_and_submit

    accounts = json.loads(
        (PROJECT_ROOT / ".runtime/demo-accounts.json").read_text(encoding="utf8")
    )
    settings = Settings(
        database_url=values["CS_TEST_DATABASE_URL"],
        query_database_url=values["CS_TEST_QUERY_DATABASE_URL"],
        expected_database="cs_agent_b0_test",
        cookie_secure=False,
        agent_worker_enabled=False,
        expiration_worker_enabled=False,
    )
    app = create_app(settings)
    report = {
        "target": "cs_agent_b0_test",
        "model": "deepseek-flash",
        "business_scope": "synthetic test only",
    }
    with TestClient(app) as client:
        origin = {"Origin": "http://127.0.0.1:5173"}
        login = client.post(
            "/api/v1/session",
            headers=origin,
            json={"login_name": "customer_a", "password": accounts["customer_a"]},
        )
        assert login.status_code == 200
        headers = {**origin, "X-CSRF-Token": login.json()["data"]["csrf_token"]}
        initial = int(client.get("/api/v1/me/wallet").json()["data"]["balance_minor"])
        assert (
            client.post(
                "/api/v1/me/topups",
                headers={**headers, **key()},
                json={"currency": "CNY", "amount_minor": "10000"},
            ).status_code
            == 200
        )
        ref = create_order(client, headers)
        order = client.get("/api/v1/me/orders/" + ref).json()["data"]
        assert (
            propose_and_submit(
                client, headers, "order.pay", ref, order["version"]
            ).status_code
            == 200
        )
        order = client.get("/api/v1/me/orders/" + ref).json()["data"]
        proposed = client.post(
            "/api/v1/action-proposals",
            headers=headers,
            json={
                "action": "order.cancel",
                "resource_ref": ref,
                "expected_version": order["version"],
                "parameters": {},
            },
        )
        assert proposed.status_code == 200
        confirmation = proposed.json()["data"]["confirmation_id"]
        stable = key()
        submitted = client.post(
            f"/api/v1/confirmations/{confirmation}/submit",
            headers={**headers, **stable},
            json={"decision": "confirm"},
        )
        assert submitted.status_code == 202
        review_id = submitted.json()["data"]["review_id"]
        worker = CancellationWorker(app.state.db, settings, Clock())
        try:
            assert worker.process_once()
        finally:
            worker.close()
        review = client.get("/api/v1/me/cancellation-reviews/" + review_id).json()[
            "data"
        ]
        result = client.get("/api/v1/me/orders/" + ref).json()["data"]
        balance = int(client.get("/api/v1/me/wallet").json()["data"]["balance_minor"])
        report.update(
            review_status=review["status"],
            decision=review["decision"],
            error_code=review.get("error_code"),
            order_status=result["trade_status"],
            refund_matches_original=result["refunded_minor"] == order["paid_minor"],
            wallet_restored=balance == initial + 10000,
            review_model=review["model_identifier"],
        )
        if review["status"] == "succeeded":
            replay = client.post(
                f"/api/v1/confirmations/{confirmation}/submit",
                headers={**headers, **stable},
                json={"decision": "confirm"},
            )
            report["replay_status"] = replay.status_code
            report["replay_no_extra_credit"] = (
                int(client.get("/api/v1/me/wallet").json()["data"]["balance_minor"])
                == balance
            )
    path = PROJECT_ROOT / ".runtime/real-cancellation.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    print(json.dumps(report, ensure_ascii=False))
    if report["review_status"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
