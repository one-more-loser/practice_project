"""Render review, verify read-only boundaries. No network or database."""
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

import markdown

OUT = Path(__file__).resolve().parent
ROOT = next(p for p in OUT.parents if (p / "backend/src/cs_agent").is_dir())
sys.path.insert(0, str(ROOT / "scripts"))
from joint_files import source_manifest


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


baseline = json.loads((OUT / "review-baseline.json").read_text(encoding="utf-8"))
current = source_manifest(ROOT)
assert current == baseline["source"], "01 source changed during review"
acceptance_changes = [name for name, expected in baseline["existing_acceptance_files"].items()
                      if not (ROOT / name).is_file() or sha(ROOT / name) != expected]
assert not acceptance_changes, acceptance_changes
refresh = json.loads((OUT / "review-input-refresh.json").read_text(encoding="utf-8"))
input_changes = [name for name, expected in refresh["final_input_hashes"].items() if sha(Path(name)) != expected]
assert not input_changes, "03 inputs changed again; reread changed versions before finalizing"
name = "01对03查询契约评审答复"
raw = (OUT / (name + ".md")).read_text(encoding="utf-8")
assert len(re.findall(r"^## Q-01-\d{2} ", raw, re.M)) == 10
assert len(re.findall(r"^\| S\d{2} \|", raw, re.M)) == 24
broken, links = [], 0
for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", raw):
    if target.startswith(("http:", "https:", "#")):
        continue
    clean = re.sub(r":\d+$", "", unquote(target))
    p = Path(clean) if re.match(r"^[A-Za-z]:/", clean) else OUT / clean
    links += 1
    if not p.is_file():
        broken.append(str(p))
assert not broken, broken
md = markdown.Markdown(extensions=["tables", "fenced_code", "toc"], extension_configs={"toc": {"toc_depth": "2"}})
body = md.convert(raw)
body = re.sub(r'href="([A-Za-z]:/[^\"]+)"', lambda m: 'href="file:///' + quote(re.sub(r":\d+$", "", m[1]), safe="/:") + '"', body)
body = re.sub(r"(<table>.*?</table>)", r'<div class="table-wrap">\1</div>', body, flags=re.S)
style = """
*{box-sizing:border-box}body{margin:0;background:#eef1f5;color:#1f2933;font:16px/1.85 'Microsoft YaHei',system-ui,sans-serif}
main{max-width:1160px;margin:28px auto;padding:42px 48px;background:white;border:1px solid #dce2ea;border-radius:12px}
nav{display:flex;gap:22px;flex-wrap:wrap;border-bottom:1px solid #dce2ea;padding-bottom:16px;margin-bottom:24px}
h1{font-size:30px;line-height:1.4}h2{font-size:23px;margin-top:38px;border-top:1px solid #dce2ea;padding-top:24px;scroll-margin-top:20px}h3{font-size:19px;margin-top:28px}
main>p:nth-of-type(2){background:#eff6ff;border-left:4px solid #31659e;padding:16px 20px}
p,li{overflow-wrap:anywhere}li{margin:8px 0}a{color:#215da6;text-underline-offset:3px;overflow-wrap:anywhere}
code{font-size:.9em;background:#f1f4f8;padding:2px 5px;border-radius:4px}pre{background:#f1f4f8;padding:18px;border-radius:8px;overflow:auto}pre code{padding:0}
.table-wrap{overflow:auto;margin:20px 0}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:11px 13px;border:1px solid #dce2ea;vertical-align:top;text-align:left;min-width:100px}th{background:#f5f7f9}
details{margin:20px 0;padding:14px 18px;background:#f6f8fb;border-radius:8px}summary{cursor:pointer}.toc>ul{padding-left:20px}
@media(max-width:760px){main{margin:0;padding:22px 18px;border:0;border-radius:0}body{font-size:15px}h1{font-size:25px}h2{font-size:21px}}
@media print{nav,details{display:none}main{border:0;margin:0;padding:0}body{background:white}pre{white-space:pre-wrap}}
"""
document = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + name + '</title><style>' + style + '</style></head><body><main><nav><a href="' + quote(name) + '.md">Markdown原稿</a><a href="offline-observations.json">离线观察证据</a><a href="actual-project-sql-request.json">完整当前请求</a></nav><details><summary>10题索引</summary>' + md.toc + '</details>' + body + '</main></body></html>'
HTMLParser().feed(document)
(OUT / (name + ".html")).write_text(document, encoding="utf-8")
tree = ET.parse(OUT / "query-unit.xml")
counts = {"cases": len(tree.findall(".//testcase")), "failures": len(tree.findall(".//testcase/failure")), "errors": len(tree.findall(".//testcase/error")), "skipped": len(tree.findall(".//testcase/skipped"))}
assert counts == {"cases": 28, "failures": 0, "errors": 0, "skipped": 0}
observations = json.loads((OUT / "offline-observations.json").read_text(encoding="utf-8"))
patterns = [r"sk-[A-Za-z0-9_-]{20,}", r"(?:postgresql|postgres)://[^\s<>\"]+:[^\s<>\"]+@"]
for path in [ROOT / "HANDOFF.MD", *OUT.glob("*.md"), *OUT.glob("*.html"), *OUT.glob("*.json"), *OUT.glob("*.py")]:
    contents = path.read_text(encoding="utf-8-sig")
    assert not any(re.search(pattern, contents) for pattern in patterns), "possible credential in " + path.name
validation = {"review_id": "CS03-CONTRACT-REVIEW-20261024-v1", "captured_at": datetime.now(UTC).isoformat(),
    "source_digest_unchanged": current["digest"], "source_files": len(current["files"]),
    "03_inputs_match_last_reviewed": not input_changes, "external_03_input_updates_reviewed": refresh["updates"],
    "03_written_by_this_review": False, "existing_acceptance_files_unchanged": len(baseline["existing_acceptance_files"]),
    "unit_results": counts, "real_validator_observations": len(observations["validator"]),
    "query_schema_digest": observations["versions"]["schema_digest"], "logical_columns": sum(observations["view_column_counts"].values()),
    "question_answers": 10, "seed_questions_reviewed": 24, "local_links_checked": links, "broken_links": [],
    "html_parsed": True, "credential_shape_matches": 0,
    "not_executed": ["business DB access", "PostgreSQL sandbox", "live HTTP services", "paid model", "training"],
    "scope": "01 written contract review, not joint protocol approval or 24-case PG acceptance"}
(OUT / "review-validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
paths = [ROOT / "HANDOFF.MD", *[p for p in OUT.iterdir() if p.is_file() and p.name != "review-index.json"]]
index = {"review_id": validation["review_id"], "captured_at": datetime.now(UTC).isoformat(),
         "scope": "new review top-level artifacts plus current 01 HANDOFF; old acceptance indexes unchanged",
         "files": {str(p.relative_to(ROOT)).replace('\\', '/'): sha(p) for p in sorted(paths)}}
(OUT / "review-index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
assert all(sha(ROOT / name) == value for name, value in index["files"].items())
print(json.dumps(validation, ensure_ascii=False))
