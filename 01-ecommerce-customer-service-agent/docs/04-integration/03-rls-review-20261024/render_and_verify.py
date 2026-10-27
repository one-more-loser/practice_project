"""Render this review without optional packages and check preservation boundaries."""
import hashlib
import html
from html.parser import HTMLParser
import importlib.metadata
import json
from pathlib import Path
import re
import sys
from datetime import UTC, datetime
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "backend/src/cs_agent").is_dir())
sys.path.insert(0, str(ROOT / "scripts"))
from joint_files import source_manifest

NAME = "01对03活跃店RLS独立复核与接入方案"
md = HERE / (NAME + ".md")
source = md.read_text(encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inline(value):
    escaped = html.escape(value)
    def code(match):
        raw = html.unescape(match[1])
        candidates = (HERE/raw, ROOT/raw, ROOT/"backend/src/cs_agent"/raw, ROOT.parent/"03-ecommerce-text2sql-finetuning"/raw)
        file = next((p for p in candidates if p.is_file()), None)
        rendered = "<code>" + match[1] + "</code>"
        return '<a href="' + file.resolve().as_uri() + '">' + rendered + '</a>' if file else rendered
    escaped = re.sub(r"`([^`]+)`", code, escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


lines = source.splitlines()
out, headings = [], []
i = 0
while i < len(lines):
    line = lines[i]
    if not line.strip():
        i += 1
        continue
    if line.startswith("```"):
        block = []
        i += 1
        while i < len(lines) and not lines[i].startswith("```"):
            block.append(lines[i])
            i += 1
        out.append("<pre><code>" + html.escape("\n".join(block)) + "</code></pre>")
        i += 1
        continue
    heading = re.match(r"^(#{1,6}) (.+)$", line)
    if heading:
        level, title = len(heading[1]), heading[2]
        anchor = "section-" + str(len(headings))
        headings.append((level,title,anchor))
        out.append(f'<h{level} id="{anchor}">{inline(title)}</h{level}>')
        i += 1
        continue
    if line.startswith("| ") and i+1<len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i+1]):
        header = [v.strip() for v in line.strip("|").split("|")]
        out.append('<div class="table-wrap"><table><thead><tr>' + ''.join("<th>"+inline(v)+"</th>" for v in header) + '</tr></thead><tbody>')
        i += 2
        while i<len(lines) and lines[i].startswith("| "):
            cells = [v.strip() for v in lines[i].strip("|").split("|")]
            out.append("<tr>" + ''.join("<td>"+inline(v)+"</td>" for v in cells) + "</tr>")
            i += 1
        out.append("</tbody></table></div>")
        continue
    bullet = re.match(r"^(- |\d+\. )(.+)$", line)
    if bullet:
        ordered = bullet[1] != "- "
        tag = "ol" if ordered else "ul"
        out.append("<"+tag+">")
        while i<len(lines):
            item = re.match(r"^(- |\d+\. )(.+)$", lines[i])
            if not item or (item[1] != "- ") != ordered:
                break
            out.append("<li>"+inline(item[2])+"</li>")
            i += 1
        out.append("</"+tag+">")
        continue
    out.append("<p>"+inline(line)+"</p>")
    i += 1

toc = '<nav aria-label="目录">' + ''.join(f'<a href="#{anchor}">{html.escape(title)}</a>' for level,title,anchor in headings if level==2) + '</nav>'
css = """
:root{color-scheme:light;--ink:#172331;--muted:#526172;--line:#dce3eb;--accent:#174d79}
*{box-sizing:border-box}body{margin:0;background:#f2f5f8;color:var(--ink);font:16px/1.85 system-ui,'Microsoft YaHei',sans-serif}
main{max-width:1140px;margin:32px auto;padding:42px 54px;background:white;border:1px solid var(--line);border-radius:12px}
h1{font-size:30px;line-height:1.4;margin:0 0 24px}h2{font-size:23px;margin:46px 0 18px;border-top:1px solid var(--line);padding-top:24px;scroll-margin-top:20px}h3{font-size:19px;margin:30px 0 12px}
p{margin:14px 0}li{margin:10px 0}a{color:var(--accent);text-underline-offset:3px;overflow-wrap:anywhere}strong{color:#142f49}
nav{display:flex;flex-wrap:wrap;gap:8px 18px;padding:18px 0;border-bottom:1px solid var(--line);margin-bottom:26px}nav a{font-size:14px}
code{font:13px/1.65 Consolas,monospace;background:#edf2f7;border-radius:4px;padding:2px 4px;overflow-wrap:anywhere}pre{background:#122536;color:#e5eef6;padding:20px;border-radius:8px;overflow:auto}pre code{background:transparent;color:inherit;padding:0;overflow-wrap:normal}
.table-wrap{overflow-x:auto;margin:20px 0}table{border-collapse:collapse;width:100%;font-size:14px;line-height:1.7}th,td{border:1px solid var(--line);padding:12px 14px;text-align:left;vertical-align:top}th{background:#eaf0f6;font-weight:650}td:first-child{min-width:110px}footer{margin-top:36px;font-size:13px;color:var(--muted)}
@media(max-width:760px){main{margin:0;padding:24px 18px;border-radius:0}h1{font-size:25px}table{min-width:680px}}@media print{body{background:white}main{border:0;margin:0;padding:0;max-width:none}nav{display:none}h2{break-after:avoid}tr{break-inside:avoid}a{color:inherit}}
"""
page = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+NAME+'</title><style>'+css+'</style></head><body><main data-source-sha256="'+sha(md)+'">'+toc+'\n'+"\n".join(out)+'<footer><a href="'+md.as_uri()+'">同内容 Markdown</a> · 仅契约复核与方案，本轮未实施业务修改。</footer></main></body></html>'
html_path = HERE / (NAME + ".html")
html_path.write_text(page, encoding="utf-8")

baseline = json.loads((HERE/"review-baseline.json").read_text(encoding="utf-8"))
current = source_manifest(ROOT)
checks = {"source":{"count":len(current["files"]),"digest":current["digest"],"unchanged":current==baseline["source"]}}
for key, base in (("03_inputs",None),("existing_acceptance_files",ROOT),("historical_migrations",ROOT/"backend/migrations/versions")):
    changes = []
    for name, expected in baseline[key].items():
        path = Path(name) if base is None else base/name
        if not path.exists() or sha(path) != expected:
            changes.append(name)
    checks[key] = {"checked":len(baseline[key]),"changed_or_missing":changes,"unchanged":not changes}

class Checker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = {}
        self.links = []
    def handle_starttag(self, tag, attrs):
        self.tags[tag] = self.tags.get(tag,0)+1
        if tag=="a":
            self.links.append(dict(attrs).get("href"))
parser = Checker()
parser.feed(page)
assert parser.tags.get("h1") == 1 and parser.tags.get("table") == 4
suite = ET.parse(HERE/"focused-unit.xml").find("testsuite")
probe = json.loads((HERE/"independent-rls-result.json").read_text(encoding="utf-8"))
checks["runtime"] = {"probe_result":probe["result"],"scenario_assertions":len(probe["cases"]),"historical_defect_controls":1,"current_regression_scenarios":len(probe["cases"])-1,"all_assertions_passed":all(v["passed"] for v in probe["cases"]),"cleanup":probe["cleanup"],"unit_tests":dict(suite.attrib)}
checks["html"] = {"source_sha256":sha(md),"h1":parser.tags.get("h1"),"tables":parser.tags.get("table"),"h2":parser.tags.get("h2"),"script_tags":parser.tags.get("script",0),"bytes":html_path.stat().st_size}
checks["captured_at"] = datetime.now(UTC).isoformat()
(HERE/"preservation-check.json").write_text(json.dumps(checks,ensure_ascii=False,indent=2), encoding="utf-8")
observation = json.loads((HERE/"03-input-observation.json").read_text(encoding="utf-8"))
checks["03_reviewed_inputs_stable"] = all(sha(Path(name))==value["reviewed_sha256"] for name,value in observation["inputs"].items())
checks["03_drift_acknowledged"] = [name for name,value in observation["inputs"].items() if value["changed_during_review"]]
(HERE/"preservation-check.json").write_text(json.dumps(checks,ensure_ascii=False,indent=2), encoding="utf-8")
assert checks["source"]["unchanged"] and all(checks[k]["unchanged"] for k in ("existing_acceptance_files","historical_migrations")) and checks["03_reviewed_inputs_stable"]
files = {p.name:sha(p) for p in sorted(HERE.iterdir()) if p.is_file() and p.name!="evidence-index.json"}
index = {"review_id":"CS03-RLS-REVIEW-20261024-v1","source_digest":current["digest"],"versions":{name:importlib.metadata.version(name) for name in ("sqlglot","psycopg","SQLAlchemy","pytest")},"handoff_sha256":sha(ROOT/"HANDOFF.MD"),"files":files,"captured_at":checks["captured_at"]}
(HERE/"evidence-index.json").write_text(json.dumps(index,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({"source_unchanged":checks["source"]["unchanged"],"source_files":len(current["files"]),"acceptance_files_unchanged":checks["existing_acceptance_files"]["checked"],"historical_migrations_unchanged":checks["historical_migrations"]["checked"],"03_reviewed_inputs_stable":checks["03_reviewed_inputs_stable"],"03_drift_acknowledged":checks["03_drift_acknowledged"],"html":checks["html"],"indexed_files":len(files)}, ensure_ascii=False))
