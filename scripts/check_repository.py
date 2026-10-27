"""Check a publishable Git snapshot without executing project code or exposing matches."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 5 * 1024 * 1024
PATTERNS = {
    'provider_token': re.compile(r'(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'credential_url': re.compile(r'(?:postgres(?:ql)?(?:\+\w+)?|mysql|redis)://[^\s/:]+:([^\s@]+)@'),
}


def git(*args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(['git', *args], cwd=ROOT, input=input_bytes, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError('git_command_failed')
    return result.stdout


def candidates(staged: bool) -> list[tuple[str, bytes]]:
    if not staged:
        names = sorted(set(git('ls-files', '-c', '-o', '--exclude-standard', '-z').decode('utf-8').split('\0')) - {''})
        return [(name, (ROOT / name).read_bytes()) for name in names]
    rows = []
    for row in git('ls-files', '--stage', '-z').split(b'\0'):
        if not row:
            continue
        metadata, name = row.split(b'\t', 1)
        mode, sha, stage = metadata.decode('ascii').split()
        if stage != '0' or mode not in ('100644', '100755'):
            raise RuntimeError('unsupported_index_entry')
        rows.append((name.decode('utf-8'), sha))
    ids = list(dict.fromkeys(sha for _, sha in rows))
    stream = io.BytesIO(git('cat-file', '--batch', input_bytes=''.join(sha + '\n' for sha in ids).encode('ascii')))
    objects = {}
    for expected in ids:
        actual, kind, size = stream.readline().decode('ascii').split()
        if actual != expected or kind != 'blob':
            raise RuntimeError('unexpected_git_object')
        objects[actual] = stream.read(int(size))
        if stream.read(1) != b'\n':
            raise RuntimeError('invalid_batch_framing')
    return [(name, objects[sha]) for name, sha in rows]


def fixture_placeholder(name: str, rule: str, match: re.Match[str]) -> bool:
    if rule != 'credential_url':
        return False
    password = match.group(1).lower()
    if any(marker in password for marker in ('example', 'password', 'replace', 'test', '{', '<', '%', 'demo', 'readonly')):
        return True
    # Reviewed synthetic connection configuration, never a live credential.
    return name == '02-ecommerce-operations-agent/backend/tests/test_int_run_entry.py' and 'synthetic' in password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', action='store_true', help='Read actual index blobs, not working-tree bytes.')
    args = parser.parse_args()
    files = candidates(args.staged)
    findings = []
    total = 0
    for name, raw in files:
        total += len(raw)
        parts = Path(name).parts
        if any(p.startswith('.venv') or p in ('.runtime', 'node_modules', '__pycache__', 'shared-models') for p in parts):
            findings.append({'path': name, 'rule': 'local_state_in_snapshot'})
        if len(raw) > MAX_BYTES:
            findings.append({'path': name, 'rule': 'oversize_file', 'bytes': len(raw)})
        if b'\0' in raw:
            continue
        for line_no, line in enumerate(raw.decode('utf-8', errors='replace').splitlines(), 1):
            for rule, pattern in PATTERNS.items():
                for match in pattern.finditer(line):
                    if not fixture_placeholder(name, rule, match):
                        findings.append({'path': name, 'line': line_no, 'rule': rule})
    print(json.dumps({'passed': not findings, 'scope': 'git_index_blobs' if args.staged else 'candidate_worktree_files', 'files': len(files), 'bytes': total, 'findings': findings}, ensure_ascii=False))
    return int(bool(findings))


if __name__ == '__main__':
    raise SystemExit(main())
