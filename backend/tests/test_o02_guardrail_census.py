"""Issue #792 guardrail census — defect-class eradication (Phase 4.2).

Three families, all value-pinning:

(a) RED at base / GREEN after the fix: the wiki and KMS compile processors
    pass str(exc) into their fail paths. The census pins that every
    ``_fail_job`` hand-off and every ``_publish_event(..., error=...)`` uses
    the module constant ``FAILED_ERROR_CODE`` whose exact value is pinned
    below — an empty string, a placeholder, or a ``str(exc)`` variant fails.
(b) GREEN at base BY DESIGN and after the fix: ``WikiStore.fail_job`` /
    ``KMSStore.fail_job`` compose the cap settlement as
    ``attempt_cap_exceeded: {error}``; driving them at the cap with the
    constant must store exactly the composed stable string. This is the
    mutation kill-switch for store-layer-redaction / prefix-stripping
    implementations that the frozen C7/C8 checks cannot catch (their
    ``attempt_cap`` assertion rides the janitor's COALESCE default, not
    fail_job's composition).
(c) RED at base / GREEN after the fix: every ``json_extract(payload_json``
    in both store files sits inside a ``CASE WHEN json_valid(payload_json)``
    guard (counts pinned per file), and both ``_compile_job_from_jobs_row``
    payload parses carry the sibling ``except (JSONDecodeError, TypeError)``
    guard — one malformed row must never raise out of a vault's job readers.
"""

import ast
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WIKI_PROCESSOR = os.path.join(BACKEND, "app", "services", "wiki_compile_processor.py")
KMS_PROCESSOR = os.path.join(BACKEND, "app", "services", "kms_compile_processor.py")
WIKI_STORE = os.path.join(BACKEND, "app", "services", "wiki_store.py")
KMS_STORE = os.path.join(BACKEND, "app", "services", "kms_store.py")

WIKI_FAILED_CODE = (
    "COMPILE_FAILED: wiki compile dispatch failed; see server logs"
)
KMS_FAILED_CODE = "COMPILE_FAILED: KMS compile dispatch failed; see server logs"

# Plan-critic-verified site census (plan v2): every json_extract(payload_json
# occurrence in each store file. A missed guard OR a newly added unguarded
# site changes one of these counts and fails the census.
WIKI_JSON_EXTRACT_SITES = 7
KMS_JSON_EXTRACT_SITES = 4

# Backward search window: every extraction must have its json_valid guard
# within this many preceding source characters (all real sites sit ~40 chars
# after their CASE line).
GUARD_WINDOW_CHARS = 250


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _module_constant_value(tree: ast.AST, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(node.value, ast.Constant) and isinstance(
                        node.value.value, str
                    ):
                        return node.value.value
                    raise AssertionError(
                        f"{name} must be a plain string literal assignment"
                    )
    raise AssertionError(f"module constant {name} not found")


def _fail_job_error_arg_is_constant(tree: ast.AST) -> list[str]:
    """Return violations: every _fail_job hand-off must pass the constant.

    The processors fail jobs via ``asyncio.to_thread(self._fail_job, job.id,
    <error>, worker)`` — the AST sees a Call whose first argument is the
    ``._fail_job`` attribute; its third positional argument must be the
    constant's name, never a Call (str(exc)) or any other expression.
    """
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Attribute) and first.attr == "_fail_job":
            if len(node.args) < 3:
                violations.append("_fail_job hand-off missing the error argument")
                continue
            error_arg = node.args[2]
            if not (isinstance(error_arg, ast.Name) and error_arg.id == "FAILED_ERROR_CODE"):
                violations.append(
                    "_fail_job error argument is not the FAILED_ERROR_CODE constant"
                )
        if isinstance(first, ast.Attribute) and first.attr == "_publish_event":
            for kw in node.keywords:
                if kw.arg == "error" and not (
                    isinstance(kw.value, ast.Name)
                    and kw.value.id == "FAILED_ERROR_CODE"
                ):
                    violations.append(
                        "_publish_event error= keyword is not the FAILED_ERROR_CODE constant"
                    )
    return violations


def _payload_parse_is_guarded(tree: ast.AST, func_name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Try):
                    continue
                calls = [
                    n
                    for n in ast.walk(sub)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "loads"
                ]
                if not calls:
                    continue
                handler_catches_decode = any(
                    "JSONDecodeError" in ast.dump(h.type)
                    for h in sub.handlers
                    if h.type is not None
                )
                if calls and handler_catches_decode:
                    return True
    return False


class TestO02GuardrailCensus(unittest.TestCase):
    """Pins the issue #792 defect classes repo-closed for the wiki/KMS jobs."""

    def test_processors_persist_only_the_stable_code(self):
        for path, pinned in ((WIKI_PROCESSOR, WIKI_FAILED_CODE), (KMS_PROCESSOR, KMS_FAILED_CODE)):
            with self.subTest(module=os.path.basename(path)):
                tree = ast.parse(_read(path))
                self.assertEqual(_module_constant_value(tree, "FAILED_ERROR_CODE"), pinned)
                self.assertEqual(
                    _fail_job_error_arg_is_constant(tree),
                    [],
                    "every _fail_job/_publish_event error site must use the constant",
                )

    def test_store_fail_job_composes_the_cap_prefix_over_the_code(self):
        from app.config import settings
        from app.models.database import run_migrations
        from app.services.job_lease import JobLease
        from app.services.kms_store import KMSStore
        from app.services.wiki_store import WikiStore

        db_path = str(Path(mkdtemp(prefix="o02-census-")) / "census.db")
        run_migrations(db_path)
        with mock.patch.object(settings, "wiki_kms_job_lease_enabled", True), \
             mock.patch.object(settings, "jobs_max_attempts", 1):
            for name, store_cls, queue, code in (
                ("wiki", WikiStore, "wiki", WIKI_FAILED_CODE),
                ("kms", KMSStore, "kms", KMS_FAILED_CODE),
            ):
                with self.subTest(store=name):
                    conn = sqlite3.connect(db_path, timeout=10)
                    conn.row_factory = sqlite3.Row
                    self.addCleanup(conn.close)
                    job = store_cls(conn).create_job(1, "manual")
                    row = JobLease(conn).claim(queue, "census-worker")
                    self.assertIsNotNone(row)
                    attempts = store_cls(conn).fail_job(
                        job.id, code, worker_id="census-worker"
                    )
                    self.assertGreaterEqual(attempts, 1)
                    stored = conn.execute(
                        "SELECT error FROM jobs WHERE id = ?", (job.id,)
                    ).fetchone()["error"]
                    self.assertEqual(
                        stored, f"attempt_cap_exceeded: {code}"
                    )

    def test_every_json_extract_site_is_json_valid_guarded(self):
        for path, expected_sites in (
            (WIKI_STORE, WIKI_JSON_EXTRACT_SITES),
            (KMS_STORE, KMS_JSON_EXTRACT_SITES),
        ):
            with self.subTest(store=os.path.basename(path)):
                source = _read(path)
                sites = source.count("json_extract(payload_json")
                guards = source.count("CASE WHEN json_valid(payload_json)")
                self.assertEqual(sites, expected_sites)
                self.assertEqual(guards, expected_sites)
                idx = source.find("json_extract(payload_json")
                while idx != -1:
                    window = source[max(0, idx - GUARD_WINDOW_CHARS):idx]
                    self.assertIn(
                        "json_valid(payload_json)",
                        window,
                        "json_extract site without a json_valid guard nearby",
                    )
                    idx = source.find("json_extract(payload_json", idx + 1)

    def test_both_row_mappers_guard_the_payload_parse(self):
        for path in (WIKI_STORE, KMS_STORE):
            with self.subTest(store=os.path.basename(path)):
                tree = ast.parse(_read(path))
                self.assertTrue(
                    _payload_parse_is_guarded(tree, "_compile_job_from_jobs_row"),
                    "_compile_job_from_jobs_row must guard json.loads(payload_json)",
                )


if __name__ == "__main__":
    unittest.main()
