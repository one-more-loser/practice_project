"""Train-only, binary business rewards over an owned temporary PostgreSQL.

Gold results may be cached. Candidate results are executed on every call.
Identity and real parameter values are injected here, never in model prompts.
"""
from __future__ import annotations

import copy
import importlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sandbox" / "runner"))
from common import load_jsonl
from scoring import strict_json, protocol_ok
from eval_business import validator01, _params, SCOPE
from validator import validate
from oracle_errors import OracleEnvironmentError, is_candidate_sql_error
from owned_pg import OwnedSandbox, cleanup_error


def equivalent(actual, expected, ordered=False):
    # JSON canonical keys handle heterogeneous SQL values and retain duplicates.
    encode = lambda rows: [json.dumps(r, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")) for r in rows]
    left, right = encode(actual), encode(expected)
    return left == right if ordered else Counter(left) == Counter(right)


class SQLReward:
    fixtures = ("fx_a", "fx_b", "fx_c")

    def __init__(self, train_rows, consumer01_root, manifest, fixtures=None):
        selected = tuple(self.fixtures if fixtures is None else fixtures)
        if (len(selected) < 2 or len(selected) != len(set(selected))
                or not set(selected) <= {'fx_a', 'fx_b', 'fx_c'}):
            raise ValueError('reward fixtures must name at least two distinct owned fixtures')
        self.fixtures = selected
        rows = load_jsonl(train_rows) if isinstance(train_rows, (str, Path)) else train_rows
        if not rows or any(r.get("split") != "train" for r in rows):
            raise ValueError("reward oracle accepts only explicitly marked train rows")
        self.rows = {r["id"]: copy.deepcopy(r) for r in rows}
        if len(self.rows) != len(rows):
            raise ValueError("duplicate training row ids")
        self.consumer01_root = Path(consumer01_root).resolve()
        self.manifest = (json.loads(Path(manifest).read_text(encoding="utf-8"))
                         if isinstance(manifest, (str, Path)) else copy.deepcopy(manifest))
        if not self.manifest.get("schema_digest"):
            raise ValueError("manifest must supply the frozen schema digest")
        self.directory = None
        self.server = None
        self.servers = []
        self.sandboxes = {}
        self.directory_identity = None
        self.connections = {}
        self.gold = {}
        self.gold_columns = {}
        self.ready = False

    def __enter__(self):
        if self.ready:
            raise RuntimeError("oracle already entered")
        try:
            query = [r for r in self.rows.values() if r["expected_kind"] == "query"]
            checked = validator01([self._validation_item(r, r["sql"]) for r in query],
                                  self.consumer01_root)
            for row, verdict in zip(query, checked):
                if not verdict["ok"] or not validate(row["sql"], row["parameter_names"]).ok:
                    raise RuntimeError(f"gold validator failed: {row['id']}")
                if sorted(_params(row)) != sorted(row["parameter_names"]):
                    raise RuntimeError(f"gold parameter mismatch: {row['id']}")
            self.directory = tempfile.mkdtemp(prefix="03-grpo-owned-pg-")
            stat = Path(self.directory).stat()
            self.directory_identity = (stat.st_dev, stat.st_ino)
            self.runner = importlib.import_module("run")
            # Explicit handles avoid mutable global PGDATA and environment state.
            import pgserver
            for fixture in self.fixtures:
                # Roles are cluster-wide; fixture setup recreates restricted
                # roles, so every fixture must own a separate cluster.
                path = Path(self.directory) / fixture
                path.mkdir(exist_ok=False)
                lease = OwnedSandbox(path)
                self.sandboxes[fixture] = lease  # retain even if creation raises
                server = lease.start(pgserver)
                self.servers.append(server)
                server.__enter__()
                conn = self.runner.psycopg.connect(server.get_uri(), autocommit=True)
                self.connections[fixture] = conn
                self.runner.setup_fixture(conn, fixture)
                for row in query:
                    result = self.runner.run_query(conn, row["sql"], _params(row), SCOPE)
                    self.gold[(row["id"], fixture)] = result["rows"]
                    self.gold_columns[(row['id'], fixture)] = result['columns']
            self.uninformative = {row['id'] for row in query
                if not any(self.gold[(row['id'], fixture)] for fixture in self.fixtures)}
            self.ready = True
            return self
        except BaseException as exc:
            try:
                self.close(original_error=exc)
            except OracleEnvironmentError:
                raise
            raise OracleEnvironmentError('reward sandbox initialization failed') from exc

    def score(self, row, generated):
        if not self.ready:
            raise RuntimeError("reward oracle is not open")
        trusted = self.rows.get(row["id"])
        if trusted is None or trusted != row:
            raise ValueError("row is not the registered training record")
        obj = strict_json(generated)
        record = {"reward": 0.0, "status": "protocol_failed"}
        if not protocol_ok(trusted, obj):
            return record
        if trusted["expected_kind"] != "query":
            return {"reward": 1.0, "status": "protocol_correct"}
        if row['id'] in getattr(self, 'uninformative', set()):
            # All-empty fixtures cannot distinguish a correct candidate from
            # an always-empty SQL. Keep the frozen data; withhold positive reward.
            return {"reward": 0.0, "status": "insufficient_fixture_discrimination"}
        params = _params(trusted)
        if sorted(params) != sorted(obj["parameter_names"]):
            return {**record, "status": "parameter_mismatch"}
        verdict3 = validate(obj["sql"], obj["parameter_names"])
        if not verdict3.ok:
            return {**record, "status": "validator03_failed", "code": verdict3.code}
        verdict1 = validator01([self._validation_item(trusted, obj["sql"])],
                               self.consumer01_root)[0]
        if not verdict1["ok"]:
            return {**record, "status": "validator01_failed"}
        matches = {}
        try:
            for fixture, conn in self.connections.items():
                result = self.runner.run_query(conn, obj["sql"], params, SCOPE)
                matches[fixture] = (result['columns'] == self.gold_columns[(row['id'], fixture)]
                    and equivalent(result['rows'], self.gold[(row['id'], fixture)], row.get('comparison') == 'ordered'))
        except Exception as exc:
            if not is_candidate_sql_error(exc):
                raise OracleEnvironmentError('reward executor environment failed; rollout must be voided') from exc
            return {**record, "status": "execution_failed", "error": str(exc)[:300]}
        return {"reward": float(all(matches.values())), "status": "executed",
                "fixture_matches": matches}

    def _validation_item(self, row, sql):
        return {"sql": sql, "schema_revision": row["schema_revision"],
                "schema_digest": self.manifest["schema_digest"]}

    def close(self, original_error=None):
        self.ready = False
        failures = []
        sandboxes = getattr(self, 'sandboxes', {})
        # Keep failed connection/fixture identities. Successful fixtures may be
        # removed individually, but never erase their failed siblings' parent.
        for fixture, lease in list(sandboxes.items()):
            try:
                lease.close(self.connections.get(fixture), original_error=original_error)
            except BaseException as exc:
                failures.append((fixture, exc))
            else:
                self.connections.pop(fixture, None)
                del sandboxes[fixture]
                if lease.server in self.servers:
                    self.servers.remove(lease.server)
        known = {id(lease.server) for lease in sandboxes.values()}
        for server in self.servers:
            if id(server) not in known:
                failures.append(('unregistered_server', RuntimeError('server has no captured project lease')))
        unknown_connections = {fixture: conn for fixture, conn in self.connections.items()
                               if fixture not in sandboxes}
        if unknown_connections:
            for fixture, conn in unknown_connections.items():
                try:
                    conn.close()
                except BaseException as exc:
                    failures.append((fixture + ':connection_close', exc))
            failures.append(('unregistered_connection', RuntimeError('connection without owned sandbox')))
        if failures:
            try:
                cleanup_error(self.directory, self.server, None,
                    {'data_directory': self.directory, 'failed_fixtures': [name for name, _ in failures]},
                    failures, original_error)
            except OracleEnvironmentError as error:
                error.failed_fixtures = tuple(name for name, _ in failures)
                error.sandboxes, error.server_handles = sandboxes, tuple(self.servers)
                error.directory = self.directory
                raise
        if self.directory is not None:
            try:
                path = Path(self.directory)
                stat = path.stat()
                if (path.is_symlink() or (stat.st_dev, stat.st_ino) != self.directory_identity):
                    raise RuntimeError('reward parent identity changed')
                # Successful lease removal must leave no unknown or diagnostic
                # files; never recursively remove an unaccounted child directory.
                if any(path.iterdir()):
                    raise RuntimeError('reward parent contains retained/unknown entries')
                path.rmdir()
                self.directory = None
            except BaseException as exc:
                cleanup_error(self.directory, None, None,
                    {'data_directory': self.directory}, [('parent_removal', exc)], original_error)
        self.server = None

    def __exit__(self, exc_type, exc_value, traceback):
        self.close(original_error=exc_value)
