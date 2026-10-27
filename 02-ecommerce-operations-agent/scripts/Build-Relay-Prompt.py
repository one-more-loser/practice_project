"""Build the user-forwarded prompt HTML; no services or business data are touched."""
import hashlib
import html
import json
from pathlib import Path

import markdown

root = Path(__file__).resolve().parents[1]
assert root.name == "02-ecommerce-operations-agent"
source = root / "docs/04-integration/发给01会话的联调提示词.md"
target = source.with_suffix(".html")
raw = source.read_text(encoding="utf-8")
body = markdown.markdown(raw, extensions=["tables", "fenced_code"])
payload = json.dumps({"text": raw}, ensure_ascii=False).replace("<", "\\u003c")
style = """
:root{color-scheme:light;--ink:#30251e;--muted:#75675d;--line:#e6ded5;--orange:#aa4a1c;--bg:#fffaf5}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.85 'Segoe UI','Microsoft YaHei',sans-serif}
.bar{position:sticky;top:0;background:#fffaf5f7;border-bottom:1px solid var(--line);padding:14px 24px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
button{background:var(--orange);border:0;color:#fff;padding:10px 20px;border-radius:8px;cursor:pointer;font:inherit}button:focus-visible,a:focus-visible{outline:3px solid #e7ab77;outline-offset:3px}
a{color:var(--orange);text-underline-offset:3px}article,footer{max-width:1020px;margin:auto;padding:28px 30px}h1{font-size:30px;line-height:1.4}h2{font-size:25px;margin-top:38px}h3{font-size:21px;margin-top:32px}p,li{overflow-wrap:anywhere}li{margin:8px 0}code{font:14px/1.7 Consolas,monospace;background:#fff0e5;padding:2px 4px}pre{white-space:pre-wrap;overflow:auto}hr{border:0;border-top:1px solid var(--line);margin:36px 0}footer{border-top:1px solid var(--line);color:var(--muted);font-size:13px}#copy-status{color:var(--muted)}
@media(max-width:680px){article,footer{padding:22px 18px}h1{font-size:25px}.bar{padding:12px 18px}}@media print{.bar{display:none}body{background:white}article{max-width:none}h2,h3{break-after:avoid}}
"""
script = """
document.getElementById('copy-prompt').addEventListener('click', async () => {
  const text = JSON.parse(document.getElementById('prompt-data').textContent).text;
  const status = document.getElementById('copy-status');
  try {
    await navigator.clipboard.writeText(text);
    status.textContent = '完整提示词已复制';
  } catch {
    const field = document.createElement('textarea');
    field.value = text;
    field.style.position = 'fixed'; field.style.left = '-10000px';
    document.body.appendChild(field); field.select();
    const copied = document.execCommand('copy'); field.remove();
    status.textContent = copied ? '完整提示词已复制' : '请打开Markdown全文复制';
  }
});
"""
page = ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>交01补全 · 新会话联合联调提示词</title><style>" + style + "</style></head><body>"
        "<div class='bar'><button id='copy-prompt' type='button'>复制完整提示词，发给01</button>"
        "<a href='发给01会话的联调提示词.md'>Markdown全文</a><span id='copy-status' role='status'></span></div>"
        "<article>" + body + "</article><footer>提示词版本 XOPS-JOINT-HANDOFF-v0.1 · 待01补全；未签收联合验收。<br>"
        "来源SHA256：" + html.escape(hashlib.sha256(source.read_bytes()).hexdigest()) + "</footer>"
        "<script id='prompt-data' type='application/json'>" + payload + "</script><script>" + script + "</script></body></html>")
target.write_text(page, encoding="utf-8")
(root / ".runtime/relay-copy-button.js").write_text(script, encoding="utf-8")
assert json.loads(payload)["text"] == raw
print(json.dumps({"html": str(target.relative_to(root)), "prompt_chars": len(raw),
                  "copy_payload_matches_markdown": True}, ensure_ascii=False))
