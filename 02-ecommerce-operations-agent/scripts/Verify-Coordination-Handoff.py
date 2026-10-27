"""Verify document-only coordination changes without renewing business test claims."""
import hashlib
import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

root = Path(__file__).resolve().parents[1]
assert root.name == "02-ecommerce-operations-agent"


def sha(path):
    return hashlib.sha256((root / path).read_bytes()).hexdigest()


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.links, self.resources = [], [], []
        self.payload, self.in_payload = "", False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])
        if tag == "script" and attrs.get("id") == "prompt-data":
            self.in_payload = True
        if tag in ("script", "img", "link"):
            self.resources.append(attrs.get("src", attrs.get("href", "")))

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_payload = False

    def handle_data(self, data):
        if self.in_payload:
            self.payload += data


prompt_path = "docs/04-integration/发给01会话的联调提示词.md"
html_path = "docs/04-integration/发给01会话的联调提示词.html"
prompt = (root / prompt_path).read_text(encoding="utf-8")
parser = Parser()
parser.feed((root / html_path).read_text(encoding="utf-8"))
assert len(parser.ids) == len(set(parser.ids))
assert json.loads(parser.payload)["text"] == prompt
assert not any(url.startswith(("https:", "http:")) for url in parser.resources)
for link in parser.links:
    if link.startswith(("http:", "https:", "#")):
        continue
    assert ((root / html_path).parent / unquote(link.split("#")[0])).exists(), link
for field in ["01现状与源码版本", "实际运行版本", "接口完整信息", "运行与配置",
              "数据库、迁移与测试隔离", "数据与样本", "验证与限制", "新会话任务及你的验收要求",
              "不能只回复", "合并", "完整提示词", "integration_run_id",
              "给01原会话的验收回传", "给02原会话的验收回传", "不启动联合实施"]:
    assert field in prompt, field

prior_path = "docs/06-acceptance/收尾复核证据.json"
prior = json.loads((root / prior_path).read_text(encoding="utf-8"))
mismatches = [path for path, expected in prior["code_sha256"].items() if sha(path) != expected]
assert not mismatches, mismatches
document_report = json.loads((root / ".runtime/requirements-document-check.json").read_text(encoding="utf-8"))
assert document_report["issues"] == []
paths = ["HANDOFF.MD", "README.md", prompt_path, html_path,
         "docs/04-integration/01回传模板.md", "docs/03-plan/02实施计划.md",
         "docs/06-acceptance/02收尾与联调交付.md", "docs/06-acceptance/02收尾与联调交付.html",
         "docs/01-requirements/需求评审.html", "docs/02-design/设计评审.html",
         ".runtime/update_review_documents.py", "scripts/Build-Relay-Prompt.py",
         "scripts/Verify-Coordination-Handoff.py"]
report = {
    "version": "XOPS-JOINT-HANDOFF-v0.1",
    "checked_at": datetime.now(timezone.utc).isoformat(),
    "scope": "document-only workflow change; 01 review and complete prompt -> new joint implementation chat -> separate original 01/02 acceptance",
    "checks": {"required_information_categories": 8, "copy_payload_matches_markdown": True,
        "prompt_chars": len(prompt), "unique_html_ids": len(parser.ids),
        "local_prompt_links": len(parser.links), "external_assets": 0,
        "copy_button_javascript": "node --check .runtime/relay-copy-button.js exit 0",
        "review_documents": document_report,
        "business_code_fingerprints_verified": len(prior["code_sha256"]),
        "business_code_mismatches": mismatches, "business_tests_rerun": False},
    "prior_business_evidence": {"path": prior_path, "sha256": sha(prior_path),
        "note": "Historical snapshot retained unchanged; its document fingerprints predate this workflow change."},
    "current_document_sha256": {path: sha(path) for path in paths},
    "not_performed": ["01 confirmation", "joint implementation or integration",
        "business code edits", "sibling/root writes", "messages to other chats", "new chat creation",
        "paid model", "production deployment", "HTML browser visual acceptance"],
}
target = root / "docs/06-acceptance/联调交接流程更新证据.json"
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"information_categories": 8, "business_code_fingerprints": len(prior["code_sha256"]),
    "business_code_changes": len(mismatches), "copy_payload_matches": True,
    "markdown_links": document_report["markdown_local_links"]}, ensure_ascii=False))
