"""联合开发回归入口：固定Debian测试目标，显式禁止所有模型。"""

import argparse
import os
import sys

from cs_agent.config import PROJECT_ROOT
from cs_agent.runtime_profiles import apply_runtime_profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["focus", "full", "unit"], default="focus")
    parser.add_argument("--evidence-label", default=None)
    options = parser.parse_args()
    label = options.evidence_label or options.suite
    if not label.replace("-", "").replace("_", "").isalnum():
        parser.error("evidence label must contain only letters, digits, dash or underscore")
    os.chdir(PROJECT_ROOT)
    apply_runtime_profile("debian")
    run = PROJECT_ROOT / ".runtime/XOPS-20261023-01"
    (run / "tmp").mkdir(exist_ok=True)
    disabled = run / "models-disabled.json"
    disabled.write_text("{}", encoding="utf-8")
    os.environ.update(CS_MODEL_SERVICES_FILE=str(disabled), CS_AGENT_WORKER_ENABLED="false",
                      TEMP=str(run / "tmp"), TMP=str(run / "tmp"))
    sys.path.insert(0, str(PROJECT_ROOT))
    if options.suite == "focus":
        paths = ["backend/tests/integration/test_operations_identity.py", "backend/tests/integration/test_operations_snapshot.py",
                 "backend/tests/integration/test_activity_ledger.py", "backend/tests/api/test_checkout.py",
                 "backend/tests/api/test_commerce_guards.py", "backend/tests/api/test_fulfillment.py",
                 "backend/tests/api/test_aftersales_transport.py", "backend/tests/integration/test_commerce_interleavings.py"]
    elif options.suite == "unit":
        paths = ["backend/tests/unit"]
    else:
        paths = ["backend/tests"]
    import pytest
    raise SystemExit(pytest.main([*paths, "-q", "--tb=short", "-p", "no:cacheprovider",
                                 "--basetemp=" + str(run / ("pytest-" + label)),
                                 "--junitxml=" + str(run / ("01-" + label + ".xml"))]))


if __name__ == "__main__":
    main()
