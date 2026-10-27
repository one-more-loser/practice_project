"""只公开聚合断言结果，不复制失败原文、凭据或个人资料。"""

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    suites = ET.parse(ROOT / ".runtime/pytest-b1.xml").getroot().findall(".//testsuite")
    counts = {
        key: sum(int(suite.get(key, "0")) for suite in suites)
        for key in ("tests", "errors", "failures", "skipped")
    }
    frontend = json.loads((ROOT / ".runtime/vitest-b1.json").read_text(encoding="utf-8"))
    browser = json.loads((ROOT / ".runtime/b1-playwright.json").read_text(encoding="utf-8"))
    assert counts["failures"] == counts["errors"] == counts["skipped"] == 0
    assert frontend["success"] and frontend["numFailedTests"] == 0
    assert browser["stats"]["unexpected"] == browser["stats"]["flaky"] == 0
    report = {
        "stage": "B1",
        "captured_at": datetime.now(UTC).isoformat(),
        "scope": "T-CS-004..008 local exit; pending user review",
        "backend": {
            "level": "unit+TestClient+real_PostgreSQL",
            "counts": counts,
            "seconds": round(sum(float(s.get("time", "0")) for s in suites), 2),
        },
        "frontend": {
            "level": "jsdom component and contract",
            "passed": frontend["numPassedTests"],
            "files": len(frontend["testResults"]),
        },
        "browser": {
            "level": "Playwright bundled Chromium + real HTTP + PostgreSQL; not complete Chrome/Edge compatibility",
            "stats": browser["stats"],
        },
        "model_calls": "not_executed",
        "cross_project_integration": "not_executed",
        "V1_acceptance": "not_completed",
    }
    review_file = ROOT / ".runtime/pytest-b1-review.xml"
    full_includes_catalog_boundary = any(
        case.get("name") == "test_product_detail_is_independent_of_catalog_first_page"
        for case in ET.parse(ROOT / ".runtime/pytest-b1.xml").getroot().iter("testcase")
    )
    if review_file.exists() and not full_includes_catalog_boundary:
        review_suites = ET.parse(review_file).getroot().findall(".//testsuite")
        review_counts = {
            key: sum(int(suite.get(key, "0")) for suite in review_suites)
            for key in ("tests", "errors", "failures", "skipped")
        }
        assert review_counts["failures"] == review_counts["errors"] == review_counts["skipped"] == 0
        report["post_review_regression"] = {
            "counts": review_counts,
            "scope": "product detail beyond first 50; public catalog; price/stock guards; shop maintenance",
            "note": "84-test full run preceded this bounded product-query correction; affected paths rechecked and browser journey repeated",
        }
    target = ROOT / "docs/06-acceptance/B1/checks.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Captured B1 assertions: backend={counts['tests']}; frontend={frontend['numPassedTests']}; browser={browser['stats']['expected']}"
    )


if __name__ == "__main__":
    main()
