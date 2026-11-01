"""Acceptance checks C9/C10 (issue #987): settings-singleton test pollution.

``tests/test_o04c_feedback_pins.py`` ``_O04cBase.setUp/tearDown`` snapshot
and restore ONLY ``settings.wiki_llm_curator_enabled``, while
``TestCuratorPathReactivatesSupersededClaim.test_curator_reemission_reactivates_
superseded_claim`` assigns SEVEN settings attrs; the other six leak on the
singleton. When ``tests/test_settings_curator.py`` runs afterwards in the
SAME pytest process, ``TestSettingsExpansion::test_put_rejects_curator_
enabled_without_url_or_model`` gets 200 instead of 422 (the leaked
url/model satisfy validation) — a Backend CI red caused purely by test
order.

* C9/AC9 (DISCRIMINATING): run the polluter test in-process (unittest
  TestLoader + TestResult, exactly as pytest would) and assert
  ``settings.wiki_llm_curator_url`` / ``wiki_llm_curator_model`` equal their
  pre-run values. RED at base (they leak), GREEN after the fix restores
  them.
* C10/AC10 (DISCRIMINATING): same in-process co-run, then execute the victim
  ``TestSettingsExpansion::test_put_rejects_curator_enabled_without_url_or_
  model`` the same way (its own setUp builds the client fixtures) and assert
  it PASSES. RED at base (200 != 422), GREEN after the fix.

Defensive hygiene (this file practices what the C11 census preaches): every
settings attr observable here is snapshotted before the imported tests run
and restored in a finally block, so these checks never pollute the process
themselves — order-independent under pytest-randomly.
"""

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
POLLUTER_PATH = TESTS_DIR / "test_o04c_feedback_pins.py"
VICTIM_PATH = TESTS_DIR / "test_settings_curator.py"

POLLUTER_CLASS = "TestCuratorPathReactivatesSupersededClaim"
POLLUTER_TEST = "test_curator_reemission_reactivates_superseded_claim"
VICTIM_CLASS = "TestSettingsExpansion"
VICTIM_TEST = "test_put_rejects_curator_enabled_without_url_or_model"

# The seven attrs the polluter test assigns; url/model are the two the
# victim's 422 depends on. users_enabled / jwt_secret_key / data_dir are
# also guarded here because the victim's setUp mutates them and a setUp
# exception would skip its tearDown.
GUARDED_FIELDS = (
    "wiki_llm_curator_enabled",
    "wiki_llm_curator_run_on_ingest",
    "wiki_llm_curator_url",
    "wiki_llm_curator_model",
    "wiki_llm_curator_mode",
    "wiki_llm_curator_require_quote_match",
    "wiki_llm_curator_require_chunk_id",
    "users_enabled",
    "jwt_secret_key",
    "data_dir",
)

_MODULE_CACHE: dict[str, object] = {}


def _load_test_module(path: Path, alias: str):
    """Load a sibling test module under a private alias (never colliding
    with pytest's own import of the same file)."""
    if alias in _MODULE_CACHE:
        return _MODULE_CACHE[alias]
    spec = importlib.util.spec_from_file_location(alias, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    _MODULE_CACHE[alias] = module
    return module


def _run_one_test(module, class_name: str, method_name: str):
    """Run exactly one unittest method via unittest.TestLoader + TestResult
    (its setUp/tearDown run, exactly as under pytest)."""
    suite = __import__("unittest").TestLoader().loadTestsFromName(
        f"{class_name}.{method_name}", module
    )
    result = __import__("unittest").TestResult()
    suite.run(result)
    return result


def _describe(result) -> str:
    parts = []
    for _case, tb in getattr(result, "failures", []) + getattr(result, "errors", []):
        parts.append(tb.strip().splitlines()[-1] if tb.strip() else "no traceback")
    return "; ".join(parts)[:400]


def test_polluter_restores_curator_url_and_model():
    """C9/AC9: after the polluter test runs in-process, url/model are
    restored. RED at base (pollution), GREEN after the fix."""
    polluter = _load_test_module(POLLUTER_PATH, "issue987_guard_polluter_mod")
    before = {f: getattr(settings, f) for f in GUARDED_FIELDS}
    try:
        result = _run_one_test(polluter, POLLUTER_CLASS, POLLUTER_TEST)
        assert result.wasSuccessful(), (
            "the polluter test itself must pass in this harness; "
            f"failures/errors: {_describe(result)}"
        )
        for field in ("wiki_llm_curator_url", "wiki_llm_curator_model"):
            leaked = getattr(settings, field)
            assert leaked == before[field], (
                f"settings singleton pollution (issue #987 AC9): "
                f"{field} leaked by {POLLUTER_CLASS}.{POLLUTER_TEST}: "
                f"before={before[field]!r} after={leaked!r} — the test's "
                "setUp/tearDown snapshot must cover every settings attr it "
                "assigns"
            )
    finally:
        # Self-defense: undo anything the imported test left behind, so THIS
        # check never pollutes the process (independent of the fix).
        for field, value in before.items():
            setattr(settings, field, value)


def test_victim_put_rejection_survives_polluter_corun():
    """C10/AC10: polluter then victim in one process — the victim's 422 must
    survive. RED at base (it fails after the polluter), GREEN after the fix."""
    polluter = _load_test_module(POLLUTER_PATH, "issue987_guard_polluter_mod")
    victim = _load_test_module(VICTIM_PATH, "issue987_guard_victim_mod")
    before = {f: getattr(settings, f) for f in GUARDED_FIELDS}
    try:
        polluter_result = _run_one_test(polluter, POLLUTER_CLASS, POLLUTER_TEST)
        assert polluter_result.wasSuccessful(), (
            "the polluter test itself must pass in this harness; "
            f"failures/errors: {_describe(polluter_result)}"
        )
        victim_result = _run_one_test(victim, VICTIM_CLASS, VICTIM_TEST)
        assert victim_result.wasSuccessful(), (
            "settings singleton pollution (issue #987 AC10): the victim "
            f"{VICTIM_CLASS}.{VICTIM_TEST} must still pass when executed "
            "after the polluter in the same process; it failed because the "
            f"polluter leaked settings attrs: {_describe(victim_result)}"
        )
    finally:
        for field, value in before.items():
            setattr(settings, field, value)
