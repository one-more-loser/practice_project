#!/usr/bin/env python
"""Build the public SFT candidate set from SQL-R1 sampled_Complex + SynSQL-2.5M DBs.

This script is the *only* code boundary for public data in this project.  It is
self-contained on purpose: ``training/common.py`` currently hard-codes the
business schema and does not know about ``generic_sqlite`` rows, so the public
prompt/answer pair is defined here as pure functions that the training side can
import (or copy) later:

    prompt_generic_sqlite(row)
    answer_generic_sqlite(row)
    completion_text_generic_sqlite(row, eos_token)

Prompt shape (``PROMPT_FORMAT_GENERIC``)::

    [SYSTEM]
    ... generic SQLite read-only instructions ...
    [SCHEMA]
    ... complete reflected schema of the attached SQLite database ...
    [EVIDENCE]
    ... external_knowledge from SQL-R1, or ``（无）`` when empty ...
    [QUESTION]
    ... natural-language question ...
    [ANSWER]

The answer is a single-line JSON object ``{"kind": "query", "sql": "...",
"parameter_names": []}``.  SQL literals are kept verbatim; ``parameter_names``
is empty because the public SQL-R1 examples are self-contained queries.

Stages
------
download   copy/verify the two pinned upstream files, extract the SQLite DBs,
           write the data card, and print the source counts.
preflight   pick 32 preflight rows (32 distinct DBs) from the screening pool by
           a stable hash and record every attempt (accepted / rejected + why).
build      group the full 5000-row source by SQL template / normalized question,
           hold out rows 4000-4999 and every group they touch, reflect schemas,
           validate single-statement read-only SQL against the attached DBs,
           filter by token length, and emit train/dev/test + manifest +
           rejected rows.  Selection is whole-group only, in stable group order,
           and never exceeds ``--public-cap`` records.
mix        business:public = 1:1 by record count for train, keeps both dev sets,
           and freezes the business test set.

Every row is ``review_status=candidate_checked_execution``: it passed
deterministic execution validation but is *not* a human-reviewed gold label.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import random
import shutil
import sqlite3
import sys
import time
import unicodedata
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]

SQLR1_COMMIT = "ccfb511e81fdb3a8e87bd3da7f3d71882cedd532"
SQLR1_URL = (
    "https://raw.githubusercontent.com/DataArcTech/SQL-R1/"
    f"{SQLR1_COMMIT}/example_data/sampled_Complex.jsonl"
)
SQLR1_FILENAME = "sqlr1-sampled_Complex.jsonl"
SQLR1_SHA256 = "6b2095b16dc1ff3b7a02c1320eee806370499c44386c27152498573f0d36bafa"
SQLR1_BYTES = 18429859

SYNSQL_REVISION = "cca2c84cc3b41afa6b51534762a6a3a4a420baca"
SYNSQL_URL = (
    "https://huggingface.co/datasets/seeklhy/SynSQL-2.5M/resolve/"
    f"{SYNSQL_REVISION}/databases.zip"
)
SYNSQL_FILENAME = "synsql-databases.zip"
SYNSQL_SHA256 = "a7ef413ae80a830b8b1c5b54a271e0c49657482fb69ae49479876b45a03dba83"
SYNSQL_BYTES = 54476172
SYNSQL_SQLITE_FILES = 16583

PROFILE = "generic_sqlite"
DIALECT = "sqlite"
SOURCE = "SQL-R1"
SOURCE_DATASET = "DataArcTech/SQL-R1"
SOURCE_FILE = "example_data/sampled_Complex.jsonl"
HOLDOUT_START = 4000
REVIEW_STATUS = "candidate_checked_execution"

SYSTEM_GENERIC = (
    "你是 SQLite 只读 SQL 候选生成器。只输出一个 JSON 对象，不要 Markdown、不要解释。\n"
    '回答格式：{"kind":"query","sql":"...","parameter_names":[]}\n'
    "- SQL 必须是单条只读 SELECT 或 WITH ... SELECT；不使用写操作、PRAGMA、ATTACH。\n"
    "- 只能引用 [SCHEMA] 中的表、列和关系，不要编造列名。\n"
    "- [EVIDENCE] 是题目给出的外部知识，可为空；它不改变只读约束。\n"
    "- 不执行 SQL，不输出推理过程。"
)

PROMPT_FORMAT_GENERIC = (
    "[SYSTEM]\n{system}\n[SCHEMA]\n{schema}\n[EVIDENCE]\n{evidence}\n"
    "[QUESTION]\n{question}\n[ANSWER]\n"
)

EMPTY_EVIDENCE = "（无）"

SPLIT_TRAIN = "train"
SPLIT_DEV = "dev"
SPLIT_TEST = "public_test"

FORBIDDEN_SQL = (
    "Insert",
    "Update",
    "Delete",
    "Create",
    "Drop",
    "Alter",
    "Command",
    "Pragma",
    "Attach",
    "Detach",
    "Copy",
    "Merge",
    "Transaction",
    "Commit",
    "Rollback",
    "Grant",
    "Revoke",
)


class ProtocolError(ValueError):
    """Raised when a reference SQL cannot be accepted as a candidate."""


class TokenTooLong(ValueError):
    """Raised when prompt+answer+EOS exceeds the configured limit."""


# ---------------------------------------------------------------------------
# Generic prompt / answer (importable by the training side)
# ---------------------------------------------------------------------------


def evidence_text(row: dict) -> str:
    """Return the external-knowledge evidence for a public row."""

    value = row.get("evidence")
    if value is None:
        value = row.get("external_knowledge") or ""
    if isinstance(value, list):
        value = "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def prompt_generic_sqlite(row: dict) -> str:
    """Build the generic-SQLite prompt for one public row."""

    evidence = evidence_text(row) or EMPTY_EVIDENCE
    schema = str(row["schema_text"]).strip()
    question = str(row["question"]).strip()
    return PROMPT_FORMAT_GENERIC.format(
        system=SYSTEM_GENERIC, schema=schema, evidence=evidence, question=question
    )


def answer_generic_sqlite(row: dict) -> str:
    """Build the single-line JSON answer for one public row."""

    payload = {
        "kind": "query",
        "sql": str(row["sql"]),
        "parameter_names": [],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def completion_text_generic_sqlite(row: dict, eos_token: str = "<|endoftext|>") -> str:
    """Answer text plus the EOS token (matches training/common.py)."""

    return answer_generic_sqlite(row) + eos_token


def prompt_digest() -> str:
    """Stable digest of the prompt contract, recorded in manifests."""

    material = SYSTEM_GENERIC + "\x00" + PROMPT_FORMAT_GENERIC
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Small IO helpers
# ---------------------------------------------------------------------------


def resolve(path: str | os.PathLike) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_jsonl(path: Path, rows: list[dict]) -> str:
    text = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
    )
    atomic_write_text(path, text)
    return sha256_bytes(text.encode("utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:  # pragma: no cover - corrupt input
                raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc
    return rows


def write_json(path: Path, payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    atomic_write_text(path, text)
    return sha256_bytes(text.encode("utf-8"))


def print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# SQL parsing / templating (sqlglot is imported lazily)
# ---------------------------------------------------------------------------

_SQLGLOT = None


def _sqlglot():
    """Import sqlglot, preferring the vendored copy used by this project."""

    global _SQLGLOT
    if _SQLGLOT is None:
        vendor = ROOT / "sandbox" / "vendor" / "sqlglot30"
        if vendor.is_dir() and str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        import sqlglot
        from sqlglot import exp

        _SQLGLOT = (sqlglot, exp)
    return _SQLGLOT


def parse_single_readonly(sql: str):
    """Parse one statement and require a read-only SELECT/CTE root."""

    sqlglot, exp = _sqlglot()
    text = (sql or "").strip()
    if not text:
        raise ProtocolError("empty_sql")
    try:
        statements = sqlglot.parse(text, dialect=DIALECT)
    except Exception as exc:  # sqlglot raises several subclasses of ParseError
        raise ProtocolError("sql_parse_error") from exc
    statements = [stmt for stmt in statements if stmt is not None]
    if len(statements) != 1:
        raise ProtocolError("not_single_statement")
    expr = statements[0]
    readonly_roots = (exp.Select, exp.Union, exp.Intersect, exp.Except)
    if not isinstance(expr, readonly_roots):
        raise ProtocolError("not_readonly_select")
    for name in FORBIDDEN_SQL:
        node_type = getattr(exp, name, None)
        if node_type is not None and expr.find(node_type) is not None:
            raise ProtocolError("forbidden_statement")
    return expr


def sql_template(expr) -> str:
    """Replace numeric/string literals with type markers, keep structure."""

    _, exp = _sqlglot()

    def replace(node):
        if isinstance(node, exp.Literal):
            return exp.Var(this="__STR__" if node.is_string else "__NUM__")
        return node

    templated = expr.copy().transform(replace)
    return templated.sql(dialect=DIALECT, normalize=True, comments=False)


def normalize_question(question: str) -> str:
    text = unicodedata.normalize("NFKC", str(question or ""))
    text = text.casefold()
    return " ".join(text.split())


def _template_id(db_id: str, template: str) -> str:
    material = f"{db_id}\x00{template}".encode("utf-8")
    return "tpl-" + hashlib.sha256(material).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def safe_db_relpath(db_id: str) -> str | None:
    """Return the exact archive path for a db_id, or None if unsafe/malformed."""

    if not isinstance(db_id, str) or not db_id:
        return None
    if "/" in db_id or "\\" in db_id or "\x00" in db_id:
        return None
    if db_id in {".", ".."}:
        return None
    return f"databases/{db_id}/{db_id}.sqlite"


def db_path_for(raw_dir: Path, db_id: str) -> Path | None:
    rel = safe_db_relpath(db_id)
    if rel is None:
        return None
    path = raw_dir / rel
    return path if path.is_file() else None


def load_source(raw_dir: Path) -> tuple[list[dict], dict]:
    """Read the pinned JSONL and compute the screening-pool statistics."""

    jsonl = raw_dir / SQLR1_FILENAME
    if not jsonl.is_file():
        raise SystemExit(f"missing {jsonl}; run --stage download first")
    rows = read_jsonl(jsonl)
    if len(rows) != 5000:
        print(f"WARNING: expected 5000 source rows, found {len(rows)}", file=sys.stderr)

    matched: list[int] = []
    missing_ids: Counter[str] = Counter()
    db_paths: dict[int, Path] = {}
    for index, row in enumerate(rows):
        path = db_path_for(raw_dir, row.get("db_id", ""))
        if path is None:
            missing_ids[str(row.get("db_id"))] += 1
        else:
            matched.append(index)
            db_paths[index] = path

    matched_dbs = {rows[index]["db_id"] for index in matched}
    holdout_rows = list(range(HOLDOUT_START, len(rows)))
    holdout_dbs = {rows[index]["db_id"] for index in holdout_rows if index < len(rows)}
    holdout_rows = [index for index in holdout_rows if index in db_paths]

    pool = [
        index
        for index in range(min(HOLDOUT_START, len(rows)))
        if index in db_paths and rows[index]["db_id"] not in holdout_dbs
    ]
    pool_dbs = {rows[index]["db_id"] for index in pool}

    stats = {
        "source_rows": len(rows),
        "matched_rows": len(matched),
        "matched_dbs": len(matched_dbs),
        "missing_rows": len(rows) - len(matched),
        "missing_db_ids": sorted(missing_ids),
        "missing_db_id_count": len(missing_ids),
        "holdout_start": HOLDOUT_START,
        "holdout_rows_total": len(rows) - min(HOLDOUT_START, len(rows)),
        "holdout_rows_with_db": len(holdout_rows),
        "holdout_dbs": len(holdout_dbs),
        "pool_rows": len(pool),
        "pool_dbs": len(pool_dbs),
        "rows_excluded_because_db_is_holdout": sum(
            1
            for index in range(min(HOLDOUT_START, len(rows)))
            if index in db_paths and rows[index]["db_id"] in holdout_dbs
        ),
    }
    return rows, {"stats": stats, "db_paths": db_paths, "pool": pool, "holdout_dbs": holdout_dbs}


def build_group_index(rows: list[dict]) -> tuple[dict, dict]:
    """Union rows sharing (db_id, SQL template) or normalized question.

    Returns ``(groups, parsed)`` where ``parsed[i]`` caches the parse result so
    later validation does not re-parse every row.
    """

    parent = list(range(len(rows)))

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    parsed: dict[int, dict] = {}
    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    question_buckets: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        try:
            expr = parse_single_readonly(row.get("sql", ""))
            template = sql_template(expr)
            parsed[index] = {
                "template": template,
                "template_id": _template_id(str(row.get("db_id")), template),
            }
        except ProtocolError as exc:
            parsed[index] = {"error": str(exc)}
        else:
            buckets[(str(row.get("db_id")), template)].append(index)
        normalized_question = normalize_question(row.get("question", ""))
        if normalized_question:
            question_buckets[normalized_question].append(index)

    for members in list(buckets.values()) + list(question_buckets.values()):
        if len(members) > 1:
            first = members[0]
            for other in members[1:]:
                union(first, other)

    members_by_root: dict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        members_by_root[find(index)].append(index)

    groups: dict[str, dict] = {}
    for members in members_by_root.values():
        members = sorted(members)
        material = "\x1f".join(str(index) for index in members).encode("utf-8")
        group_id = "grp-" + hashlib.sha256(material).hexdigest()[:20]
        groups[group_id] = {
            "group_id": group_id,
            "members": members,
            "db_ids": sorted({str(rows[index].get("db_id")) for index in members}),
            "size": len(members),
        }
    return groups, parsed


def split_for_group(group_id: str, seed: int) -> str:
    bucket = int(hashlib.sha256(f"{seed}:{group_id}".encode("utf-8")).hexdigest(), 16) % 100
    if bucket < 80:
        return SPLIT_TRAIN
    if bucket < 90:
        return SPLIT_DEV
    return SPLIT_TEST


def group_views(
    rows: list[dict],
    groups: dict[str, dict],
    pool: list[int],
    holdout_dbs: set[str],
    seed: int,
) -> dict:
    """Annotate groups with split/holdout state and gather size statistics."""

    pool_set = set(pool)
    holdout_members: list[int] = []
    pool_in_holdout_groups: list[int] = []
    sizes: Counter[int] = Counter()
    split_groups: Counter[str] = Counter()
    split_rows: Counter[str] = Counter()
    split_pool_rows: Counter[str] = Counter()
    for group_id, group in groups.items():
        members = group["members"]
        group["split"] = split_for_group(group_id, seed)
        group["holdout"] = any(index >= HOLDOUT_START for index in members) or any(
            rows[index].get("db_id") in holdout_dbs for index in members
        )
        group["pool_members"] = [index for index in members if index in pool_set]
        sizes[len(members)] += 1
        split_groups[group["split"]] += 1
        split_rows[group["split"]] += len(members)
        split_pool_rows[group["split"]] += len(group["pool_members"])
        if group["holdout"]:
            holdout_members.extend(members)
            pool_in_holdout_groups.extend(group["pool_members"])

    annotated = {
        "groups_total": len(groups),
        "group_sizes": dict(sorted(sizes.items())),
        "holdout_groups": sum(1 for group in groups.values() if group["holdout"]),
        "holdout_group_members": len(holdout_members),
        "pool_rows_in_holdout_groups": len(pool_in_holdout_groups),
        "pool_rows_in_holdout_groups_ids": pool_in_holdout_groups,
        "splits": {
            split: {
                "groups": split_groups[split],
                "rows": split_rows[split],
                "pool_rows": split_pool_rows[split],
            }
            for split in (SPLIT_TRAIN, SPLIT_DEV, SPLIT_TEST)
        },
    }
    return annotated


# ---------------------------------------------------------------------------
# Schema reflection
# ---------------------------------------------------------------------------


def sqlite_uri(path: Path) -> str:
    return "file:" + urllib.request.pathname2url(str(path.resolve())) + "?mode=ro"


def reflect_schema(db_path: Path, db_id: str) -> str:
    """Reflect every table/view column plus primary/foreign keys."""

    connection = sqlite3.connect(sqlite_uri(db_path), uri=True, timeout=5.0)
    try:
        objects = connection.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
        ).fetchall()
        lines = [f"Database: {db_id} (SQLite)"]
        for name, kind in objects:
            quoted = '"' + str(name).replace('"', '""') + '"'
            lines.append(f"{str(kind).upper()} {quoted}")
            for column in connection.execute(f"PRAGMA table_info({quoted})").fetchall():
                column_name, column_type = column[1], column[2]
                parts = [f"  {column_name}"]
                if column_type:
                    parts.append(str(column_type))
                if column[5]:
                    parts.append("PRIMARY KEY")
                if column[3]:
                    parts.append("NOT NULL")
                lines.append(" ".join(parts))
            if str(kind) == "table":
                for fk in connection.execute(f"PRAGMA foreign_key_list({quoted})").fetchall():
                    lines.append(
                        f"  FOREIGN KEY ({fk[3]}) REFERENCES {fk[2]}({fk[4]})"
                    )
        return "\n".join(lines)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Read-only execution
# ---------------------------------------------------------------------------


def execute_readonly(
    db_path: Path,
    sql: str,
    timeout_seconds: float = 2.0,
    max_rows: int = 1000,
) -> dict:
    """Execute a validated statement with a wall-clock timeout and row cap."""

    connection = sqlite3.connect(sqlite_uri(db_path), uri=True, timeout=5.0)
    connection.text_factory = lambda raw: raw.decode("utf-8", "replace")
    deadline = time.monotonic() + timeout_seconds

    def progress_handler() -> int:
        return 1 if time.monotonic() > deadline else 0

    connection.set_progress_handler(progress_handler, 1000)
    started = time.monotonic()
    try:
        cursor = connection.execute(sql)
        fetched = cursor.fetchmany(max_rows + 1)
        elapsed = time.monotonic() - started
        columns = [item[0] for item in (cursor.description or [])]
        return {
            "returned_rows": len(fetched),
            "columns": columns,
            "elapsed_ms": round(elapsed * 1000.0, 1),
            "too_many_rows": len(fetched) > max_rows,
        }
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------


def load_tokenizer(path: str):
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        path, local_files_only=True, trust_remote_code=True
    )
    if tokenizer.eos_token is None:
        raise SystemExit(f"tokenizer at {path} has no eos_token")
    return tokenizer


def tokenizer_info(tokenizer, path: str) -> dict:
    try:
        version = importlib.metadata.version("transformers")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        version = None
    return {
        "path": str(path),
        "class": type(tokenizer).__name__,
        "vocab_size": len(tokenizer),
        "eos_token": tokenizer.eos_token,
        "eos_token_id": tokenizer.eos_token_id,
        "model_max_length": int(tokenizer.model_max_length or 0),
        "transformers_version": version,
        "local_files_only": True,
    }


def measure_tokens(tokenizer, row: dict, max_seq_len: int) -> dict:
    prompt_ids = tokenizer(prompt_generic_sqlite(row), add_special_tokens=False)["input_ids"]
    completion_ids = tokenizer(answer_generic_sqlite(row), add_special_tokens=False)["input_ids"]
    if not completion_ids or completion_ids[-1] != tokenizer.eos_token_id:
        completion_ids.append(tokenizer.eos_token_id)
    total = len(prompt_ids) + len(completion_ids)
    if total > max_seq_len:
        raise TokenTooLong(
            f"prompt={len(prompt_ids)} answer={len(completion_ids)} total={total} > {max_seq_len}"
        )
    return {
        "prompt": len(prompt_ids),
        "completion": len(completion_ids),
        "total": total,
        "max_seq_len": max_seq_len,
    }


# ---------------------------------------------------------------------------
# Validation shared by preflight / build
# ---------------------------------------------------------------------------


def validate_row(
    rows: list[dict],
    index: int,
    db_path: Path,
    schema_cache: dict[str, str],
    tokenizer,
    max_seq_len: int,
    timeout_seconds: float,
    max_rows: int,
    parsed: dict[int, dict],
) -> tuple[bool, list[str], dict]:
    """Run the deterministic checks for one row.

    Order: fields -> parse/protocol -> schema -> execution -> token budget.
    All checks run even when an early check fails so the report is diagnostic.
    """

    row = rows[index]
    reasons: list[str] = []
    detail: dict = {}

    db_id = str(row.get("db_id") or "")
    question = str(row.get("question") or "").strip()
    sql = str(row.get("sql") or "").strip()
    if not db_id or not question or not sql:
        reasons.append("missing_field")
        detail["fields"] = {"db_id": bool(db_id), "question": bool(question), "sql": bool(sql)}

    parse_info = parsed.get(index) or {}
    if "template" not in parse_info:
        reasons.append(parse_info.get("error", "sql_parse_error"))
        detail["protocol"] = parse_info.get("error", "sql_parse_error")
    else:
        detail["template_id"] = parse_info["template_id"]
        detail["template"] = parse_info["template"]
        detail["protocol"] = "ok"

    schema = schema_cache.get(db_id)
    if schema is None:
        try:
            schema = reflect_schema(db_path, db_id)
        except sqlite3.Error as exc:
            reasons.append("schema_reflection_error")
            detail["schema"] = f"{type(exc).__name__}: {exc}"
            schema = ""
        schema_cache[db_id] = schema
    if not schema or schema.count("\n") == 0:
        if "schema_reflection_error" not in reasons:
            reasons.append("schema_empty")
    else:
        detail["schema_chars"] = len(schema)
        detail["schema_sha256"] = sha256_bytes(schema.encode("utf-8"))

    if "template_id" in parse_info and question:
        payload = {
            "schema_text": schema,
            "question": question,
            "sql": sql,
            "evidence": row.get("external_knowledge") or "",
        }
        try:
            execution = execute_readonly(
                db_path, sql, timeout_seconds=timeout_seconds, max_rows=max_rows
            )
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "interrupt" in message:
                reasons.append("query_timeout")
                detail["execution"] = {"status": "timeout", "error": str(exc)}
            else:
                reasons.append("query_error")
                detail["execution"] = {"status": "error", "error": str(exc)}
        except sqlite3.Error as exc:
            reasons.append("query_error")
            detail["execution"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        else:
            if execution["too_many_rows"]:
                reasons.append("query_row_limit")
                detail["execution"] = {
                    "status": "row_limit",
                    "returned_rows": execution["returned_rows"],
                    "columns": execution["columns"],
                }
            else:
                detail["execution"] = execution
                detail["empty_result"] = execution["returned_rows"] == 0
        try:
            detail["tokens"] = measure_tokens(tokenizer, payload, max_seq_len)
        except TokenTooLong as exc:
            reasons.append("token_budget")
            detail["tokens"] = {"error": str(exc), "max_seq_len": max_seq_len}

    return (not reasons), reasons, detail


def build_public_row(
    rows: list[dict],
    index: int,
    schema_text: str,
    detail: dict,
    group: dict,
    split: str,
) -> dict:
    source = rows[index]
    question = str(source["question"]).strip()
    sql = str(source["sql"]).strip()
    evidence = str(source.get("external_knowledge") or "").strip()
    execution = detail.get("execution", {})
    return {
        "id": f"pub-{index:05d}",
        "profile": PROFILE,
        "dialect": DIALECT,
        "source": SOURCE,
        "source_dataset": SOURCE_DATASET,
        "source_commit": SQLR1_COMMIT,
        "source_file": SOURCE_FILE,
        "revision": SYNSQL_REVISION,
        "source_row_id": index,
        "db_id": str(source["db_id"]),
        "schema_text": schema_text,
        "schema_sha256": sha256_bytes(schema_text.encode("utf-8")),
        "evidence": evidence,
        "question": question,
        "sql": sql,
        "expected_kind": "query",
        "parameter_names": [],
        "parameters": {},
        "template_id": detail.get("template_id"),
        "template": detail.get("template"),
        "group_id": group["group_id"],
        "group_size": group["size"],
        "split": split,
        "review_status": REVIEW_STATUS,
        "empty_result": bool(detail.get("empty_result", False)),
        "execution": {
            "status": "ok",
            "returned_rows": execution.get("returned_rows"),
            "columns": execution.get("columns"),
        },
        "token_count": detail.get("tokens"),
    }


# ---------------------------------------------------------------------------
# Stage: download
# ---------------------------------------------------------------------------


def copy_or_download(
    url: str, expected_sha: str, expected_bytes: int, cache_dir: Path, dest: Path
) -> dict:
    """Prefer the local evidence cache; verify SHA256 before and after copy."""

    dest.parent.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / dest.name
    if dest.is_file() and sha256_file(dest) == expected_sha:
        size = dest.stat().st_size
        if not expected_bytes or size == expected_bytes:
            return {
                "path": str(dest),
                "source": "existing",
                "sha256": expected_sha,
                "bytes": size,
            }
    if cached.is_file():
        actual = sha256_file(cached)
        if actual != expected_sha:
            raise SystemExit(f"cache hash mismatch for {cached}: {actual} != {expected_sha}")
        shutil.copyfile(cached, dest)
        source = "cache"
    else:
        tmp = dest.with_name(dest.name + ".partial")
        with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        actual = sha256_file(tmp)
        if actual != expected_sha:
            tmp.unlink()
            raise SystemExit(f"download hash mismatch for {url}: {actual} != {expected_sha}")
        os.replace(tmp, dest)
        source = "download"
    actual = sha256_file(dest)
    if actual != expected_sha:
        raise SystemExit(f"copied file hash mismatch for {dest}: {actual} != {expected_sha}")
    size = dest.stat().st_size
    if expected_bytes and size != expected_bytes:
        raise SystemExit(f"size mismatch for {dest}: {size} != {expected_bytes}")
    return {"path": str(dest), "source": source, "sha256": actual, "bytes": size}


def extract_databases(raw_dir: Path, zip_path: Path, zip_sha: str) -> dict:
    databases = raw_dir / "databases"
    marker = raw_dir / "databases.extracted.json"
    existing = sum(1 for _ in databases.rglob("*.sqlite")) if databases.is_dir() else 0
    if marker.is_file():
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            recorded = {}
        if recorded.get("zip_sha256") == zip_sha and existing == recorded.get("sqlite_files"):
            return {"extracted": False, "sqlite_files": existing, "skipped": True}
    if existing == SYNSQL_SQLITE_FILES:
        write_json(
            marker,
            {"zip_sha256": zip_sha, "sqlite_files": existing, "extracted_at": utcnow()},
        )
        return {"extracted": False, "sqlite_files": existing, "skipped": True}

    with zipfile.ZipFile(zip_path) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise SystemExit(f"corrupt zip entry: {bad}")
        archive.extractall(raw_dir)
    count = sum(1 for _ in databases.rglob("*.sqlite"))
    if count != SYNSQL_SQLITE_FILES:
        raise SystemExit(f"expected {SYNSQL_SQLITE_FILES} sqlite files, found {count}")
    write_json(
        marker,
        {"zip_sha256": zip_sha, "sqlite_files": count, "extracted_at": utcnow()},
    )
    return {"extracted": True, "sqlite_files": count, "skipped": False}


def write_data_card(raw_dir: Path, cache_dir: Path) -> Path:
    card = raw_dir / "DATA_CARD.md"
    cached_readme = cache_dir / "synsql-readme.md"
    license_dir = raw_dir / "licenses"
    license_dir.mkdir(parents=True, exist_ok=True)
    if cached_readme.is_file():
        shutil.copyfile(cached_readme, license_dir / "SynSQL-2.5M-dataset-card.md")
    text = f"""# 公共数据卡：SQL-R1 sampled_Complex + SynSQL-2.5M SQLite

本目录只保存两份固定上游数据，供 `training/prepare_public.py` 复现公共 SFT 候选集。
生成时间：{utcnow()}

## 1. 成分与固定版本

| 角色 | 数据集 | 版本/提交 | 文件 | 字节数 | SHA256 |
| --- | --- | --- | --- | --- | --- |
| 问题+参考SQL | DataArcTech/SQL-R1 `example_data/sampled_Complex.jsonl` | commit `{SQLR1_COMMIT}` | `{SQLR1_FILENAME}` | {SQLR1_BYTES} | `{SQLR1_SHA256}` |
| 配套数据库 | seeklhy/SynSQL-2.5M `databases.zip` | revision `{SYNSQL_REVISION}` | `{SYNSQL_FILENAME}` | {SYNSQL_BYTES} | `{SYNSQL_SHA256}` |

- `sampled_Complex.jsonl`：5,000 条 Complex 难度样本，字段 `db_id / sql_complexity / question_style / question / external_knowledge / cot / sql`。
- `databases.zip`：16,583 个 SQLite 数据库（`databases/<db_id>/<db_id>.sqlite`），解压约 936 MB。
- 只使用 `question / external_knowledge / sql / db_id`；**Cot 字段完全不用**（不训练 CoT，也不把 CoT 写入任何产物）。
- 不下载 SynSQL 的 9.36 GB 全量 `data.json`，不引入任何新教师模型或付费 API。

## 2. 许可与归属

- **SynSQL-2.5M**：Hugging Face 数据集卡在 revision `{SYNSQL_REVISION}` 标注 `license: apache-2.0`，
  数据集由公开 LLM 生成（2,544,390 样本 / 16,583 DB）。原始数据集卡副本见
  `licenses/SynSQL-2.5M-dataset-card.md`。使用时保留该声明与归属。
- **SQL-R1**：上游仓库为 Apache-2.0（仓库级 `LICENSE`），commit `{SQLR1_COMMIT}` 下
  **没有独立数据卡**。因此本项目把 SQL-R1 的许可证据等级标注为“仓库级 LICENSE”，
  不把代码许可冒充为数据许可；如后续发布派生数据，需重新核对上游数据条款。
- 归属：SQL-R1（DataArcTech）、SynSQL-2.5M / OmniSQL（RUC KBReasoning 等）。
  相关论文与仓库链接见上游 README。

## 3. 使用边界

- 行号 4000–4999 及其 `db_id` 为 `source_holdout`：只用于泄漏检查，绝不进入训练。
  任何与 holdout 行同组（同库同 SQL 模板或同规范化问题）的组也整体移出训练候选。
- 公共行 `review_status=candidate_checked_execution`：只表示字段/协议/只读执行/token 预算
  通过确定性校验，**不是人工金标**，也不代表 SQL 语义与问题完全一致。
- 参考 SQL 原样保留字面量，`parameter_names` 为空；schema 为整库反射（不按 gold SQL 裁表）。
- 重新分发时必须保留本数据卡、Apache-2.0 声明与上游归属。
"""
    atomic_write_text(card, text)
    return card


def cmd_download(args) -> int:
    raw_dir = resolve(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    if not args.cache_dir:
        raise SystemExit("--cache-dir is required for --stage download")
    cache_dir = resolve(args.cache_dir)

    jsonl_info = copy_or_download(
        SQLR1_URL, SQLR1_SHA256, SQLR1_BYTES, cache_dir, raw_dir / SQLR1_FILENAME
    )
    zip_info = copy_or_download(
        SYNSQL_URL, SYNSQL_SHA256, SYNSQL_BYTES, cache_dir, raw_dir / SYNSQL_FILENAME
    )
    extraction = extract_databases(raw_dir, raw_dir / SYNSQL_FILENAME, SYNSQL_SHA256)
    card = write_data_card(raw_dir, cache_dir)

    rows, context = load_source(raw_dir)
    stats = context["stats"]
    manifest = {
        "schema": "public-raw-v1",
        "created_at": utcnow(),
        "source_rows": len(rows),
        "files": {"question_sql": jsonl_info, "databases_zip": zip_info},
        "extraction": extraction,
        "data_card": {"path": str(card), "sha256": sha256_file(card)},
        "source_stats": stats,
        "holdout_policy": "rows 4000-4999 and their db_ids are source_holdout",
        "cot_used": False,
    }
    manifest_path = raw_dir / "download-manifest.json"
    write_json(manifest_path, manifest)
    print_json(
        {
            "stage": "download",
            "raw_dir": str(raw_dir),
            "rows": len(rows),
            "matched_rows": stats["matched_rows"],
            "matched_dbs": stats["matched_dbs"],
            "pool_rows": stats["pool_rows"],
            "pool_dbs": stats["pool_dbs"],
            "extracted_sqlite": extraction["sqlite_files"],
            "manifest": str(manifest_path),
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Stage: preflight
# ---------------------------------------------------------------------------


def stable_hash(seed: int, tag: str, source_row_id: int) -> str:
    return hashlib.sha256(f"{seed}:{tag}:{source_row_id}".encode("utf-8")).hexdigest()


def select_preflight_rows(
    rows: list[dict], selectable: list[int], sample_size: int, seed: int
) -> list[int]:
    ranked = sorted(selectable, key=lambda index: stable_hash(seed, "preflight", index))
    picked: list[int] = []
    seen_dbs: set[str] = set()
    for index in ranked:
        db_id = str(rows[index]["db_id"])
        if db_id in seen_dbs:
            continue
        picked.append(index)
        seen_dbs.add(db_id)
        if len(picked) >= sample_size:
            break
    return picked


def cmd_preflight(args) -> int:
    raw_dir = resolve(args.raw_dir)
    output_dir = resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows, context = load_source(raw_dir)
    stats = context["stats"]
    groups, parsed = build_group_index(rows)
    row_to_group = {
        index: group for group in groups.values() for index in group["members"]
    }
    views = group_views(rows, groups, context["pool"], context["holdout_dbs"], args.seed)
    if views["pool_rows_in_holdout_groups"]:
        print(
            f"NOTE: {views['pool_rows_in_holdout_groups']} pool rows sit in holdout-touched "
            "groups and are excluded from preflight sampling",
            file=sys.stderr,
        )
    excluded = set(views["pool_rows_in_holdout_groups_ids"])
    selectable = [index for index in context["pool"] if index not in excluded]
    picked = select_preflight_rows(rows, selectable, args.sample_size, args.seed)

    tokenizer = load_tokenizer(args.tokenizer)
    schema_cache: dict[str, str] = {}
    attempts: list[dict] = []
    accepted_rows: list[dict] = []
    for attempt_no, index in enumerate(picked, 1):
        db_id = str(rows[index]["db_id"])
        db_path = context["db_paths"][index]
        ok, reasons, detail = validate_row(
            rows,
            index,
            db_path,
            schema_cache,
            tokenizer,
            args.max_seq_len,
            args.query_timeout,
            args.max_fetch_rows,
            parsed,
        )
        attempt = {
            "attempt": attempt_no,
            "source_row_id": index,
            "db_id": db_id,
            "split": row_to_group[index]["split"],
            "accepted": ok,
            "reasons": reasons,
            "checks": {
                "fields": "missing_field" not in reasons,
                "schema": not any(reason.startswith("schema") for reason in reasons),
                "protocol": detail.get("protocol") == "ok",
                "execution": not any(reason.startswith("query") for reason in reasons),
                "token_budget": "token_budget" not in reasons,
            },
            "detail": detail,
        }
        attempts.append(attempt)
        if ok:
            group = row_to_group[index]
            accepted_rows.append(
                build_public_row(rows, index, schema_cache[db_id], detail, group, group["split"])
            )

    attempts_path = output_dir / "attempts.jsonl"
    accepted_path = output_dir / "accepted_rows.jsonl"
    write_jsonl(attempts_path, attempts)
    write_jsonl(accepted_path, accepted_rows)
    summary = {
        "schema": "public-preflight-v1",
        "created_at": utcnow(),
        "seed": args.seed,
        "sample_size": args.sample_size,
        "pool_rows": stats["pool_rows"],
        "pool_dbs": stats["pool_dbs"],
        "pool_rows_in_holdout_groups": views["pool_rows_in_holdout_groups"],
        "selectable_rows": len(selectable),
        "attempts": len(attempts),
        "distinct_dbs": len({attempt["db_id"] for attempt in attempts}),
        "accepted": len(accepted_rows),
        "rejected": len(attempts) - len(accepted_rows),
        "reasons": dict(
            sorted(Counter(reason for item in attempts for reason in item["reasons"]).items())
        ),
        "tokenizer": tokenizer_info(tokenizer, args.tokenizer),
        "max_seq_len": args.max_seq_len,
        "token_stats": _token_stats(accepted_rows),
        "empty_result_rows": sum(1 for row in accepted_rows if row["empty_result"]),
        "files": {
            "attempts": {"path": str(attempts_path), "sha256": sha256_file(attempts_path)},
            "accepted_rows": {
                "path": str(accepted_path),
                "sha256": sha256_file(accepted_path),
            },
        },
        "note": "32 attempts are not required to all pass; failures are recorded with reasons.",
    }
    summary_path = output_dir / "summary.json"
    write_json(summary_path, summary)
    print_json(
        {
            "stage": "preflight",
            "attempts": len(attempts),
            "distinct_dbs": summary["distinct_dbs"],
            "accepted": len(accepted_rows),
            "rejected": summary["rejected"],
            "reasons": summary["reasons"],
            "summary": str(summary_path),
        }
    )
    return 0


def _token_stats(rows: list[dict]) -> dict:
    totals = [row["token_count"]["total"] for row in rows if row.get("token_count")]
    if not totals:
        return {"n": 0}
    totals.sort()
    return {
        "n": len(totals),
        "min": totals[0],
        "p50": totals[len(totals) // 2],
        "max": totals[-1],
        "max_seq_len": rows[0]["token_count"].get("max_seq_len"),
    }


# ---------------------------------------------------------------------------
# Stage: build
# ---------------------------------------------------------------------------


def _rejection(index: int, rows: list[dict], reason: str, detail=None, group=None) -> dict:
    group = group or {}
    return {
        "stage": "build",
        "source_row_id": index,
        "db_id": str(rows[index].get("db_id")),
        "group_id": group.get("group_id"),
        "split": group.get("split"),
        "reason": reason,
        "detail": detail,
    }


def cmd_build(args) -> int:
    raw_dir = resolve(args.raw_dir)
    output_dir = resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows, context = load_source(raw_dir)
    stats = context["stats"]
    db_paths = context["db_paths"]
    holdout_dbs = context["holdout_dbs"]
    pool = context["pool"]

    groups, parsed = build_group_index(rows)
    row_to_group = {index: group for group in groups.values() for index in group["members"]}
    views = group_views(rows, groups, pool, holdout_dbs, args.seed)

    # ---- structural exclusions (partition every non-pool row exactly once) ----
    rejected: list[dict] = []
    rejection_reasons: Counter[str] = Counter()

    def reject(index: int, reason: str, detail=None) -> None:
        rejected.append(_rejection(index, rows, reason, detail, row_to_group.get(index)))
        rejection_reasons[reason] += 1

    for index, row in enumerate(rows):
        if index >= HOLDOUT_START:
            reject(index, "source_holdout_row", {"reason": "row index >= 4000"})
        elif index not in db_paths:
            reject(index, "db_not_found", {"db_id": row.get("db_id")})
        elif row.get("db_id") in holdout_dbs:
            reject(index, "source_holdout_db", {"db_id": row.get("db_id")})

    # ---- carry-over of holdout groups (pool rows should never appear here) ----
    for group in groups.values():
        if not group["holdout"]:
            continue
        for index in group["pool_members"]:
            reject(index, "holdout_group_coupling")

    tokenizer = load_tokenizer(args.tokenizer)
    tokenizer_meta = tokenizer_info(tokenizer, args.tokenizer)
    schema_cache: dict[str, str] = {}

    accepted: list[dict] = []
    accepted_group_ids: set[str] = set()
    processed_group_ids: set[str] = set()
    evaluated_groups = 0
    evaluated_rows = 0
    skipped_for_capacity: list[str] = []
    stopped_by_cap = False

    for group_id in sorted(groups):
        group = groups[group_id]
        if group["holdout"] or not group["pool_members"]:
            continue
        if len(accepted) >= args.public_cap:
            stopped_by_cap = True
            break
        processed_group_ids.add(group_id)
        evaluated_groups += 1
        evaluated_rows += len(group["pool_members"])

        results = []
        for index in group["pool_members"]:
            ok, reasons, detail = validate_row(
                rows,
                index,
                db_paths[index],
                schema_cache,
                tokenizer,
                args.max_seq_len,
                args.query_timeout,
                args.max_fetch_rows,
                parsed,
            )
            results.append((index, ok, reasons, detail))

        if not all(item[1] for item in results):
            for index, ok, reasons, detail in results:
                for reason in reasons or ["group_rejected_due_to_member"]:
                    rejected.append(_rejection(index, rows, "group_" + reason, detail, group))
                    rejection_reasons["group_" + reason] += 1
            continue

        if len(accepted) + len(results) > args.public_cap:
            skipped_for_capacity.append(group_id)
            for index, _ok, _reasons, _detail in results:
                rejected.append(
                    _rejection(index, rows, "capacity_group_would_exceed_cap", None, group)
                )
                rejection_reasons["capacity_group_would_exceed_cap"] += 1
            continue

        for index, _ok, _reasons, detail in results:
            accepted.append(
                build_public_row(
                    rows, index, schema_cache[str(rows[index]["db_id"])], detail, group, group["split"]
                )
            )
        accepted_group_ids.add(group_id)

    by_split = defaultdict(list)
    for row in accepted:
        by_split[row["split"]].append(row)
    for split in (SPLIT_TRAIN, SPLIT_DEV, SPLIT_TEST):
        by_split[split].sort(key=lambda row: (row["group_id"], row["source_row_id"]))

    files = {}
    for split in (SPLIT_TRAIN, SPLIT_DEV, SPLIT_TEST):
        path = output_dir / f"{split}.jsonl"
        digest = write_jsonl(path, by_split[split])
        files[split] = {"path": str(path), "sha256": digest, "rows": len(by_split[split])}
    rejected_path = output_dir / "rejected.jsonl"
    write_jsonl(rejected_path, rejected)
    files["rejected"] = {
        "path": str(rejected_path),
        "sha256": sha256_file(rejected_path),
        "rows": len(rejected),
    }

    candidate_groups = [
        group for group in groups.values() if not group["holdout"] and group["pool_members"]
    ]
    remaining_groups = [
        group for group in candidate_groups if group["group_id"] not in processed_group_ids
    ]

    manifest = {
        "schema": "public-sft-v1",
        "created_at": utcnow(),
        "profile": PROFILE,
        "dialect": DIALECT,
        "review_status": REVIEW_STATUS,
        "review_status_meaning": (
            "字段/单条只读协议/2s 只读执行/行数上限/2048 token 预算均通过确定性校验；"
            "不是人工金标，不代表 SQL 与问题语义完全等价。"
        ),
        "source": {
            "dataset": SOURCE_DATASET,
            "file": SOURCE_FILE,
            "commit": SQLR1_COMMIT,
            "url": SQLR1_URL,
            "sha256": SQLR1_SHA256,
            "source_rows": stats["source_rows"],
            "matched_rows": stats["matched_rows"],
            "matched_dbs": stats["matched_dbs"],
            "missing_db_ids": stats["missing_db_ids"],
            "databases": {
                "dataset": "seeklhy/SynSQL-2.5M",
                "revision": SYNSQL_REVISION,
                "file": "databases.zip",
                "sha256": SYNSQL_SHA256,
                "sqlite_files": SYNSQL_SQLITE_FILES,
            },
            "cot_used": False,
        },
        "holdout": {
            "rule": "rows [4000,5000) and their db_ids are source_holdout",
            "rows": stats["holdout_rows_total"],
            "rows_with_db": stats["holdout_rows_with_db"],
            "dbs": stats["holdout_dbs"],
            "pool_rows": stats["pool_rows"],
            "pool_dbs": stats["pool_dbs"],
            "rows_excluded_because_db_is_holdout": stats[
                "rows_excluded_because_db_is_holdout"
            ],
        },
        "grouping": {
            "rule": (
                "same db_id AND (same normalized SQL template OR same normalized question) "
                "are unioned over the full 5000-row source"
            ),
            "template_rule": (
                "sqlglot sqlite parse; numeric literals -> __NUM__, string literals -> __STR__; "
                "structure/identifiers/operators retained"
            ),
            "split_rule": (
                f"sha256('{args.seed}:'+group_id) integer mod 100: 0-79 train, 80-89 dev, "
                "90-99 public_test; split before selection; groups never split"
            ),
            **{key: value for key, value in views.items() if key != "splits"},
            "splits": views["splits"],
        },
        "execution_check": {
            "timeout_seconds": args.query_timeout,
            "row_cap": args.max_fetch_rows,
            "fetched_upper_bound": args.max_fetch_rows + 1,
            "mode": "sqlite read-only URI (mode=ro), progress handler against monotonic clock",
            "empty_result_allowed": True,
        },
        "token_budget": {
            "max_seq_len": args.max_seq_len,
            "rule": "prompt_tokens + answer_tokens(+EOS) <= max_seq_len, no truncation",
            "tokenizer": tokenizer_meta,
        },
        "selection": {
            "public_cap": args.public_cap,
            "accepted": len(accepted),
            "accepted_groups": len(accepted_group_ids),
            "evaluated_groups": evaluated_groups,
            "evaluated_rows": evaluated_rows,
            "stopped_by_cap": stopped_by_cap,
            "capacity_skipped_groups": skipped_for_capacity,
            "not_evaluated_groups": len(remaining_groups),
            "not_evaluated_rows": sum(len(group["pool_members"]) for group in remaining_groups),
            "order": "groups sorted by group_id; whole group accepted/rejected; no split across groups",
        },
        "counts": {
            "train": len(by_split[SPLIT_TRAIN]),
            "dev": len(by_split[SPLIT_DEV]),
            "public_test": len(by_split[SPLIT_TEST]),
            "accepted_total": len(accepted),
            "rejected_total": len(rejected),
            "empty_result": sum(1 for row in accepted if row["empty_result"]),
            "unique_dbs": len({row["db_id"] for row in accepted}),
            "unique_groups": len(accepted_group_ids),
        },
        "per_split": {
            split: {
                "rows": len(by_split[split]),
                "groups": len({row["group_id"] for row in by_split[split]}),
                "dbs": len({row["db_id"] for row in by_split[split]}),
                "empty_result": sum(1 for row in by_split[split] if row["empty_result"]),
                "token_stats": _token_stats(by_split[split]),
            }
            for split in (SPLIT_TRAIN, SPLIT_DEV, SPLIT_TEST)
        },
        "rejected_by_reason": dict(sorted(rejection_reasons.items())),
        "prompt": {
            "format": PROMPT_FORMAT_GENERIC,
            "system": SYSTEM_GENERIC,
            "prompt_digest": prompt_digest(),
            "answer": '{"kind":"query","sql":"...","parameter_names":[]}',
            "eos_token": tokenizer.eos_token,
        },
        "files": files,
        "training_integration": {
            "status": "manual",
            "note": (
                "training/common.py 目前只有 customer_self 分支；训练前必须为 generic_sqlite "
                "增加同样的 prompt/answer 逻辑（可复制本文件的 prompt_generic_sqlite/"
                "answer_generic_sqlite），否则 mix 后的 train 无法被 train_sft.py 正确编码。"
            ),
        },
        "limitations": [
            "参考 SQL 来自上游合成数据，仅做执行校验，未做人工语义复核。",
            "SQL 字面量原样保留，parameter_names 恒为空。",
            "schema_text 为整库反射，不按 gold SQL 裁表，因此可能包含与问题无关的表。",
            "external_knowledge 仅作为 evidence 保留，未改写问题。",
            "空结果允许进入候选，并逐行标记 empty_result。",
        ],
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)
    print_json(
        {
            "stage": "build",
            "accepted": len(accepted),
            "train": len(by_split[SPLIT_TRAIN]),
            "dev": len(by_split[SPLIT_DEV]),
            "public_test": len(by_split[SPLIT_TEST]),
            "evaluated_groups": evaluated_groups,
            "rejected": len(rejected),
            "rejected_by_reason": manifest["rejected_by_reason"],
            "empty_result": manifest["counts"]["empty_result"],
            "stopped_by_cap": stopped_by_cap,
            "manifest": str(manifest_path),
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Stage: mix
# ---------------------------------------------------------------------------


def parse_ratio(text: str) -> tuple[int, int]:
    try:
        left, right = str(text).replace("：", ":").split(":")
        ratio = (int(left), int(right))
    except (ValueError, AttributeError) as exc:
        raise SystemExit(f"invalid --business-public-ratio: {text}") from exc
    if ratio[0] <= 0 or ratio[1] <= 0:
        raise SystemExit(f"invalid --business-public-ratio: {text}")
    if ratio != (1, 1):
        raise SystemExit("this pipeline only implements business:public = 1:1 (per the runbook)")
    return ratio


def _business_rows(path: Path, split: str) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"missing business {split} file: {path}")
    rows = read_jsonl(path)
    for row in rows:
        if not row.get("profile"):
            raise SystemExit(f"business {split} row {row.get('id')} lacks profile")
        if row.get("profile") != "customer_self":
            raise SystemExit(f"business {split} row {row.get('id')} has unexpected profile")
    return rows


def cmd_mix(args) -> int:
    business_dir = resolve(args.business_dir)
    public_dir = resolve(args.public_dir)
    output_dir = resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    parse_ratio(args.business_public_ratio)

    public = {
        split: read_jsonl(public_dir / f"{split}.jsonl")
        for split in (SPLIT_TRAIN, SPLIT_DEV, SPLIT_TEST)
    }
    business = {
        split: _business_rows(business_dir / f"{split}.jsonl", split)
        for split in (SPLIT_TRAIN, SPLIT_DEV, "test")
    }
    public_train = public[SPLIT_TRAIN]
    if not public_train:
        print(
            "ERROR: public_sft/train.jsonl is empty; refusing to emit a mixed dataset",
            file=sys.stderr,
        )
        return 3
    if any(row.get("profile") != PROFILE for row in public_train):
        raise SystemExit("public train contains rows with a non-generic_sqlite profile")
    if any(row.get("split") != SPLIT_TRAIN for row in public_train):
        raise SystemExit("public train contains rows whose split is not train")

    business_train = business[SPLIT_TRAIN]
    if not business_train:
        raise SystemExit("business train set is empty")

    n_public = len(public_train)
    sampling = [business_train[index % len(business_train)] for index in range(n_public)]
    random.Random(args.seed).shuffle(sampling)

    # Rows are copied verbatim (no annotation): provenance is profile, which is
    # generic_sqlite for public rows and customer_self for business rows.
    mixed_train: list[dict] = []
    for public_row, business_row in zip(public_train, sampling):
        mixed_train.append(public_row)
        mixed_train.append(business_row)

    mixed_dev = list(business[SPLIT_DEV]) + list(public[SPLIT_DEV])
    mixed_test = list(business["test"]) + list(public[SPLIT_TEST])

    # ---- invariants ----
    if mixed_test[: len(business["test"])] != business["test"]:
        raise SystemExit("business test rows were altered while mixing")
    if mixed_dev[: len(business[SPLIT_DEV])] != business[SPLIT_DEV]:
        raise SystemExit("business dev rows were altered while mixing")
    train_source_ids = {
        (row.get("source", ""), row.get("source_row_id")) for row in public_train
    }
    for split in (SPLIT_DEV, SPLIT_TEST):
        overlap = train_source_ids & {
            (row.get("source", ""), row.get("source_row_id")) for row in public[split]
        }
        if overlap:
            raise SystemExit(f"public {split} overlaps public train: {len(overlap)} rows")
    test_ids = {row.get("id") for row in business["test"]}
    if test_ids & {row.get("id") for row in mixed_train}:
        raise SystemExit("business test ids leaked into mixed train")
    for name, rows in (("dev", mixed_dev), ("test", mixed_test)):
        ids = [row.get("id") for row in rows]
        if len(ids) != len(set(ids)):
            raise SystemExit(f"duplicate ids inside mixed {name}")
    seen_ids = Counter(row.get("id") for row in mixed_train)
    duplicated_business_ids = sorted(
        key for key, count in seen_ids.items() if count > 1
    )
    unique_business_ids = len({row.get("id") for row in sampling})

    files = {}
    for name, rows in (
        ("train", mixed_train),
        ("dev", mixed_dev),
        ("test", mixed_test),
    ):
        path = output_dir / f"{name}.jsonl"
        digest = write_jsonl(path, rows)
        files[name] = {"path": str(path), "sha256": digest, "rows": len(rows)}

    frozen_dir = output_dir / "frozen"
    frozen_dir.mkdir(parents=True, exist_ok=True)
    frozen_path = frozen_dir / "business_test.jsonl"
    shutil.copyfile(business_dir / "test.jsonl", frozen_path)
    frozen_sha = sha256_file(frozen_path)
    source_test_sha = sha256_file(business_dir / "test.jsonl")
    if frozen_sha != source_test_sha:
        raise SystemExit("frozen business test copy differs from the source file")

    manifest = {
        "schema": "sft-mixed-v1",
        "created_at": utcnow(),
        "seed": args.seed,
        "ratio": "1:1",
        "method": "concatenate/reorder only; every row is copied verbatim (profile marks provenance)",
        "policy": (
            "train = public_train n rows + business_train cycled then shuffled n rows "
            f"(unique business rows = {len(business_train)}, sampled business rows = n); "
            "dev keeps business and public dev separately; "
            "test = frozen business test + public_test; no test row in train"
        ),
        "counts": {
            "train": len(mixed_train),
            "dev": len(mixed_dev),
            "test": len(mixed_test),
            "public_train": len(public_train),
            "business_train_unique": len(business_train),
            "business_train_sampling": len(sampling),
            "unique_rows": unique_business_ids,
            "sampling_rows": len(sampling),
            "duplicate_business_rows": max(0, len(sampling) - unique_business_ids),
            "duplicated_business_ids": duplicated_business_ids,
        },
        "profiles": {
            "public": PROFILE,
            "business": "customer_self",
        },
        "dev": {
            "business": len(business[SPLIT_DEV]),
            "public": len(public[SPLIT_DEV]),
        },
        "test": {
            "business": len(business["test"]),
            "public": len(public[SPLIT_TEST]),
            "business_frozen_sha256": frozen_sha,
            "business_source_sha256": source_test_sha,
            "business_frozen_path": str(frozen_path),
        },
        "inputs": {
            "business": {
                split: {
                    "path": str(business_dir / f"{split}.jsonl"),
                    "sha256": sha256_file(business_dir / f"{split}.jsonl"),
                    "rows": len(business[split]),
                }
                for split in (SPLIT_TRAIN, SPLIT_DEV, "test")
            },
            "public": {
                split: {
                    "path": str(public_dir / f"{split}.jsonl"),
                    "sha256": sha256_file(public_dir / f"{split}.jsonl"),
                    "rows": len(public[split]),
                }
                for split in (SPLIT_TRAIN, SPLIT_DEV, SPLIT_TEST)
            },
        },
        "files": files,
        "invariants": {
            "rows_copied_verbatim": True,
            "no_public_dev_test_in_train": True,
            "no_business_test_in_train": True,
            "no_duplicate_ids_in_dev_or_test": True,
            "business_test_frozen_copy_sha256": frozen_sha,
            "business_test_source_sha256": source_test_sha,
            "equal_public_business_train_counts": len(public_train) == len(sampling),
        },
        "review_status": {
            "public": REVIEW_STATUS,
            "business": "candidate_unreviewed",
        },
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)
    print_json(
        {
            "stage": "mix",
            "train": len(mixed_train),
            "dev": len(mixed_dev),
            "test": len(mixed_test),
            "public_train": len(public_train),
            "business_sampling_rows": len(sampling),
            "business_unique_rows": len(business_train),
            "business_unique_ids_sampled": unique_business_ids,
            "manifest": str(manifest_path),
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build public SFT candidates from SQL-R1 + SynSQL-2.5M (no training).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--stage", required=True, choices=["download", "preflight", "build", "mix"])
    parser.add_argument("--cache-dir", default=None, help="local evidence cache for download")
    parser.add_argument("--raw-dir", default="data/raw/sqlr1-synsql")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--sample-size", type=int, default=32)
    parser.add_argument("--seed", type=int, default=20261024)
    parser.add_argument("--tokenizer", default="/tmp/qwen3-8b-base-tokenizer")
    parser.add_argument("--max-seq-len", type=int, default=2048)
    parser.add_argument("--public-cap", type=int, default=500)
    parser.add_argument("--business-dir", default="data/sft")
    parser.add_argument("--public-dir", default="data/public_sft")
    parser.add_argument("--business-public-ratio", default="1:1")
    parser.add_argument("--query-timeout", type=float, default=2.0)
    parser.add_argument("--max-fetch-rows", type=int, default=1000)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.stage == "download":
        return cmd_download(args)
    if args.stage == "preflight":
        args.output_dir = args.output_dir or "outputs/preflight/public32"
        return cmd_preflight(args)
    if args.stage == "build":
        args.output_dir = args.output_dir or "data/public_sft"
        return cmd_build(args)
    if args.stage == "mix":
        args.output_dir = args.output_dir or "data/sft_mixed"
        return cmd_mix(args)
    parser.error(f"unknown stage: {args.stage}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
