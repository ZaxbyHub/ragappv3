"""Issue #792 guardrail census — defect-class eradication (Phase 4.2 + feedback round).

Value-pinning second layer over the behavioral o02 checks (the behavioral
tests are shape-agnostic and bite real regressions; this census adds
shape-level pins so a refactor cannot silently drop the invariants):

(a) The wiki and KMS compile processors pass the module constant
    ``FAILED_ERROR_CODE`` (exact value pinned below) into every
    ``_fail_job`` hand-off and every ``_publish_event(error=...)`` — the
    empty string, a placeholder, or ``str(exc)`` fails. Call shapes are
    matched for BOTH the direct attribute call (``self._publish_event(...)``)
    and the executor hand-off (``asyncio.to_thread(self._fail_job, ...)``).
(b) GREEN by design at base and head: ``WikiStore.fail_job`` /
    ``KMSStore.fail_job`` compose the cap settlement as
    ``attempt_cap_exceeded: {error}``; driving them at the cap with the
    constant must store exactly the composed stable string. Kill-switch for
    store-layer-redaction / prefix-stripping implementations the behavioral
    checks cannot catch.
(c) Every ``json_extract(payload_json`` in both store files sits on a source
    line that also carries ``THEN`` (the THEN branch of the
    ``CASE WHEN json_valid(payload_json)`` guard), with per-file occurrence
    counts pinned — a removed guard, a decoy comment, or a degenerate
    ``THEN 1`` guard leaves the extract on an unguarded line and fails.
(d) Both ``_compile_job_from_jobs_row`` payload parses catch the widened
    tuple (TypeError / ValueError / RecursionError — ValueError subsumes
    JSONDecodeError) AND degrade non-dict payloads via an
    ``isinstance(payload, dict)`` check, so the whole malformed-or-scalar
    class degrades instead of raising.
"""

import ast
import os
import shutil
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

# Plan-critic-verified site census: every json_extract(payload_json
# occurrence in each store file. A removed guard OR a newly added unguarded
# site changes one of these counts and fails the census.
WIKI_JSON_EXTRACT_SITES = 7
KMS_JSON_EXTRACT_SITES = 4


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


def _callee_attribute_name(node: ast.Call) -> str:
    """Attribute name of the fail/publish callee, for both call shapes.

    ``self._publish_event(...)`` / ``self._fail_job(...)`` are direct
    attribute calls (node.func); ``asyncio.to_thread(self._fail_job, ...)``
    passes the bound method as the first positional argument. Any other
    call (including the to_thread wrapper itself) returns "".
    """
    for candidate in (node.func, node.args[0] if node.args else None):
        if isinstance(candidate, ast.Attribute) and candidate.attr in (
            "_fail_job",
            "_publish_event",
        ):
            return candidate.attr
    return ""


def _error_argument(node: ast.Call, callee: str):
    """The error argument for a fail/publish call, or None.

    Direct ``_fail_job(job_id, error, worker)`` keeps the error at
    position 1; the executor form ``to_thread(self._fail_job, job_id,
    error, worker)`` shifts it to position 2. Publish errors ride the
    ``error=`` keyword (None when the event carries no error — nothing to
    pin in that case).
    """
    if callee == "_fail_job":
        # executor form (to_thread(self._fail_job, ...)): node.func is
        # to_thread, not the bound method, so the error shifts to index 2
        direct = isinstance(node.func, ast.Attribute) and node.func.attr == callee
        index = 1 if direct else 2
        return node.args[index] if len(node.args) > index else None
    for kw in node.keywords:
        if kw.arg == "error":
            return kw.value
    return None


def _fail_path_violations(tree: ast.AST) -> list:
    """Violations: every _fail_job/_publish_event error must be the constant.

    Also fails when NO such site exists at all — a refactor that renames the
    helpers must update this census, not silence it vacuously.
    """
    violations = []
    site_count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee_attribute_name(node)
        if not callee:
            continue
        site_count += 1
        error_arg = _error_argument(node, callee)
        if error_arg is None and callee == "_publish_event":
            continue  # non-failure events carry no error field
        if not (
            isinstance(error_arg, ast.Name)
            and error_arg.id in ("FAILED_ERROR_CODE", "PAYLOAD_MALFORMED_ERROR")
        ):
            violations.append(
                f"{callee} error argument at line {node.lineno} is not a pinned constant"
            )
    if site_count == 0:
        violations.append(
            "no _fail_job/_publish_event call sites found — the census is vacuous"
        )
    return violations


def _payload_parse_is_guarded(tree: ast.AST, func_name: str) -> bool:
    """True when func_name degrades the payload for the whole bad-JSON class.

    Requires BOTH: (1) a try around a json.loads call whose handlers include
    ValueError-or-JSONDecodeError and RecursionError, and (2) an
    ``isinstance(payload, dict)`` degradation so valid-JSON scalars/arrays
    degrade too (issue #792 feedback — the exception tuple alone misses the
    non-object class).
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == func_name):
            continue
        guarded_parse = False
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
            dumped = " ".join(
                ast.dump(h.type) for h in sub.handlers if h.type is not None
            )
            if ("ValueError" in dumped or "JSONDecodeError" in dumped) and (
                "RecursionError" in dumped
            ):
                guarded_parse = True
        shape_check = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "isinstance"
            and n.args
            and isinstance(n.args[0], ast.Name)
            and n.args[0].id == "payload"
            and len(n.args) > 1
            and isinstance(n.args[1], ast.Name)
            and n.args[1].id == "dict"
            for n in ast.walk(node)
        )
        if guarded_parse and shape_check:
            return True
    return False


class TestO02GuardrailCensus(unittest.TestCase):
    """Pins the issue #792 defect classes repo-closed for the wiki/KMS jobs."""

    def test_processors_persist_only_the_stable_code(self):
        for path, pinned in (
            (WIKI_PROCESSOR, WIKI_FAILED_CODE),
            (KMS_PROCESSOR, KMS_FAILED_CODE),
        ):
            with self.subTest(module=os.path.basename(path)):
                tree = ast.parse(_read(path))
                self.assertEqual(
                    _module_constant_value(tree, "FAILED_ERROR_CODE"), pinned
                )
                self.assertEqual(
                    _module_constant_value(tree, "PAYLOAD_MALFORMED_ERROR"),
                    "PAYLOAD_MALFORMED: compile job payload is corrupt; "
                    "see server logs",
                )
                self.assertEqual(
                    _fail_path_violations(tree),
                    [],
                    "every _fail_job/_publish_event error must use a pinned constant",
                )

    def test_store_fail_job_composes_the_cap_prefix_over_the_code(self):
        from app.config import settings
        from app.models.database import run_migrations
        from app.services.job_lease import JobLease
        from app.services.kms_store import KMSStore
        from app.services.wiki_store import WikiStore

        db_path = str(Path(mkdtemp(prefix="o02-census-")) / "census.db")
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), ignore_errors=True)
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

    def test_every_json_extract_site_is_then_guarded(self):
        """Line-level pairing: every extract sits on a THEN line of its guard.

        Stronger than proximity: a decoy comment, a moved guard, or a
        degenerate ``THEN 1`` leaves the extract on a line without THEN and
        fails, independent of what sits 250 characters earlier.
        """
        for path, expected_sites in (
            (WIKI_STORE, WIKI_JSON_EXTRACT_SITES),
            (KMS_STORE, KMS_JSON_EXTRACT_SITES),
        ):
            with self.subTest(store=os.path.basename(path)):
                source = _read(path)
                lines = source.splitlines()
                sites = source.count("json_extract(payload_json")
                guards = source.count("CASE WHEN json_valid(payload_json)")
                self.assertEqual(sites, expected_sites)
                self.assertEqual(guards, expected_sites)
                unguarded = [
                    lineno
                    for lineno, line in enumerate(lines, start=1)
                    if "json_extract(payload_json" in line and "THEN" not in line
                ]
                self.assertEqual(
                    unguarded,
                    [],
                    "json_extract(payload_json site(s) outside a THEN branch: "
                    f"lines {unguarded}",
                )

    def test_both_row_mappers_guard_the_payload_parse(self):
        for path in (WIKI_STORE, KMS_STORE):
            with self.subTest(store=os.path.basename(path)):
                tree = ast.parse(_read(path))
                self.assertTrue(
                    _payload_parse_is_guarded(tree, "_compile_job_from_jobs_row"),
                    "_compile_job_from_jobs_row must catch "
                    "(TypeError, ValueError, RecursionError) AND degrade "
                    "non-dict payloads via isinstance(payload, dict)",
                )


if __name__ == "__main__":
    unittest.main()
