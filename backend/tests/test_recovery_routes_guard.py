"""Guard suite for the shared recovery-route exemption predicate (issue #1058).

PR #1057 left three guard-divergence classes open:

* ``_extract_local_sets`` in test_exempt_paths_guard.py only collects
  ``ast.Constant`` members of ``ast.Assign`` set literals, so an exemption
  grown post-assignment (``.add(...)`` / ``|=``) is invisible to the guard;
* ``maintenance._EXEMPT_POST_PATHS`` + ``method == "POST"`` diverges from
  deps.py's method-agnostic ``exempt_paths`` local set (PATCH/PUT/DELETE
  answers differ between the two gates);
* guard canonicalization treated router-local (``/auth/...``) and mounted
  (``/api/auth/...``) spellings interchangeably while runtime deps.py
  matches only the mounted request path, so a router-local-only entry is
  dead at runtime but guard-green.

The frozen target contract (issue #1058): ONE shared method-aware
canonicalizing predicate ``is_recovery_exempt(method, path)`` in
``backend/app/security/recovery_routes.py``, consumed by BOTH
``backend/app/api/deps.py`` and ``backend/app/middleware/maintenance.py``,
behavior-neutral for today's four deps.py exempt paths (:507-512) and
today's maintenance exemptions.

At the base tree this module fails to collect (ImportError — the predicate
module lands with the fix); that collection error IS the recorded base
evidence. After the fix this suite is GREEN and keeps biting: reverting
either consumer to a local set, growing the exemption set unconsciously,
dropping method-awareness, or dropping canonicalization fails loudly here.

The mutant legs follow the simulation style of
``test_guard_bites_on_disagreeing_sets`` (test_exempt_paths_guard.py): each
sub-leg simulates one divergence mutant and asserts the GUARD'S OWN
DETECTOR — the same ``_recovery_divergence_findings`` logic this suite runs
against the real predicate — reports it with an actionable message.
Because the detector probes the predicate BEHAVIORALLY over a pinned
(method, path) matrix, a mutant is caught regardless of the code shape used
to grow the set (the exact ``.add()`` blind spot of the old AST extractor).
"""

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CSRF_TEST_POLICY = "naive"

from app.security.recovery_routes import is_recovery_exempt  # noqa: E402

# (method, path) pairs that MUST stay exempt. The two recovery surfaces are
# exempt today under BOTH gates and under BOTH spellings (deps.py :507-512
# carries router-local and mounted spellings and answers method-agnostically;
# maintenance exempts POST on both) — so POST on either spelling of
# change-password/login must answer True in any behavior-neutral, canonicalizing
# predicate.
PINNED_EXEMPT = [
    ("POST", "/api/auth/change-password"),
    ("POST", "/auth/change-password"),
    ("POST", "/api/auth/login"),
    ("POST", "/auth/login"),
]

# (method, path) pairs that MUST stay non-exempt. Every entry is blocked for
# flagged users in deps.py today (consciously-blocked /auth routes — see
# BLOCKED_AUTH_ROUTES in test_exempt_paths_guard.py — or non-auth paths) and
# maintenance-blocked where the method mutates, so True here is an
# unconscious behavior change, not a recovery surface.
PINNED_BLOCKED = [
    ("POST", "/api/auth/register"),
    ("GET", "/api/auth/register"),
    ("POST", "/api/auth/sessions"),
    ("DELETE", "/api/auth/sessions/7"),
    # Only GET /auth/me is me-read exempt in deps.py today: every other
    # method on /auth/me stays blocked for flagged users (and mutation
    # methods are maintenance-blocked) — this pair is also the probe that
    # kills a method-blind predicate.
    ("PATCH", "/api/auth/me"),
    ("POST", "/api/auth/me"),
    ("PATCH", "/api/vaults/1"),
    ("POST", "/api/auth/probe-unclassified"),
]


def _recovery_divergence_findings(pred) -> list[str]:  # noqa: ANN001
    """The guard's own divergence detector: every loud, actionable finding it
    would raise about ``pred(method, path)``. Applied to the REAL predicate
    (must return []) and to each simulated mutant (must return findings)."""
    findings: list[str] = []
    for method, path in PINNED_EXEMPT:
        answer = pred(method, path)
        if answer is not True:
            findings.append(
                f"recovery regression: is_recovery_exempt({method!r}, {path!r}) "
                f"returned {answer!r}, but this recovery surface is exempt "
                "today under both gates (deps.py exempt_paths :507-512 / "
                "maintenance _EXEMPT_POST_PATHS) — a router-local-only entry "
                "or a lost canonicalization makes the exemption dead at "
                "runtime (issue #1058 leg c)"
            )
    for method, path in PINNED_BLOCKED:
        answer = pred(method, path)
        if answer is not False:
            findings.append(
                f"unconscious exemption growth: is_recovery_exempt({method!r}, "
                f"{path!r}) returned {answer!r}, but this (method, path) is "
                "blocked for flagged users and/or maintenance-blocked today — "
                "grow the exemption set ONLY with a deliberate classification "
                "decision (a BLOCKED_AUTH_ROUTES-style entry plus a pinned "
                "pair), whatever code shape carries it (issue #1058 legs a/b)"
            )
    return findings


def _canonical(path: str) -> str:
    return path if path.startswith("/api") else f"/api{path}"


def _mutant_post_assignment_growth(method: str, path: str) -> bool:
    """Mutant (a) — missed shape: the exemption set grew AFTER its literal
    assignment via ``.add(...)`` (the exact shape the old AST-only extractor
    could not see), onto an unclassified auth route and a non-recovery
    route, and answers for every mutating method."""
    exempt = {"/auth/change-password", "/auth/login"}
    exempt.add("/api/auth/probe-unclassified")  # invisible to Assign-only AST
    exempt.add("/api/vaults/1")  # grown onto a non-recovery route
    return _canonical(path) in exempt and method in (
        "POST",
        "PATCH",
        "PUT",
        "DELETE",
    )


def _mutant_method_divergence(method: str, path: str) -> bool:
    """Mutant (b) — method divergence: method-blind membership, the
    pre-#1058 shape where one gate exempts a path for POST only while the
    shared predicate answers for every method (PATCH/PUT/DELETE divergence
    between the two gates).

    CHECK_WRONG amendment (issue #1043/#1058 trace): the originally frozen
    body stored only router-local spellings but probed canonicalized
    (``/api/...``) inputs, so every membership test answered False — the
    mutant behaved as leg (c) and could never produce the method-divergence
    finding for any correct predicate. The faithful simulation is today's
    deps.py gate itself: method-agnostic membership over BOTH spellings, so
    PATCH/POST on /api/auth/me answer True (unconscious growth) exactly the
    way a method-blind predicate diverges."""
    exempt = {
        "/auth/change-password",
        "/api/auth/change-password",
        "/auth/login",
        "/api/auth/login",
        "/auth/me",
        "/api/auth/me",
    }
    return (path or "/").rstrip("/") in exempt  # `method` ignored entirely


def _mutant_router_local_only(method: str, path: str) -> bool:
    """Mutant (c) — router-local-only spelling: entries spelled router-local
    ONLY, matched by raw membership with no canonicalization — dead at
    runtime (deps.py matches the mounted /api/auth/... request path) yet
    green under the old interchangeably-canonicalizing guard comparison."""
    exempt = {"/auth/change-password", "/auth/login"}
    return method == "POST" and path in exempt


class TestRecoveryRoutesGuard(unittest.TestCase):
    """The #1058 divergence classes must fail loudly, forever."""

    def test_divergence_mutants_fail_loudly(self):
        """RED at base (module absent — collection error), GREEN after the
        fix: the REAL predicate passes the guard's detector, and each of the
        three #1058 divergence mutants is rejected by the SAME detector with
        an actionable message."""
        # The fixed-tree green condition: the real shared predicate answers
        # the whole pinned matrix exactly (behavior-neutral for today's four
        # exempt paths and today's blocked surfaces, method-aware, and
        # canonicalizing across router-local/mounted spellings).
        real_findings = _recovery_divergence_findings(is_recovery_exempt)
        self.assertEqual(
            real_findings,
            [],
            "the shared predicate diverges from today's pinned exemption "
            "behavior — fix the predicate, or change the pinned matrix ONLY "
            "with a conscious classification decision: "
            + "; ".join(real_findings),
        )

        # Sub-leg (a) — missed-shape/post-assignment exemption growth: the
        # guard must catch a set grown via `.add()` onto unclassified
        # surfaces even though no Assign-literal ever named them.
        growth_findings = _recovery_divergence_findings(
            _mutant_post_assignment_growth
        )
        self.assertTrue(
            growth_findings,
            "guard blind spot: an exemption grown post-assignment "
            "(`.add()`) onto unclassified routes must fail the guard loudly "
            "— the old AST-only extractor shape must never come back",
        )
        self.assertTrue(
            any("probe-unclassified" in f for f in growth_findings),
            "the growth finding must be actionable: name the grown "
            f"(method, path) — got {growth_findings}",
        )
        self.assertTrue(
            any("/api/vaults/1" in f for f in growth_findings),
            "the growth finding must name the non-recovery route the set "
            f"grew onto — got {growth_findings}",
        )

        # Sub-leg (b) — method divergence: a method-blind predicate (the
        # pre-#1058 maintenance-vs-deps shape) must fail the guard loudly.
        method_findings = _recovery_divergence_findings(_mutant_method_divergence)
        self.assertTrue(
            method_findings,
            "guard blind spot: a method-blind exemption predicate must fail "
            "the guard loudly — PATCH/PUT/DELETE answers may never diverge "
            "between the two gates again",
        )
        self.assertTrue(
            any("PATCH" in f and "/api/auth/me" in f for f in method_findings),
            "the method-divergence finding must be actionable: name the "
            f"(method, path) whose answer changed — got {method_findings}",
        )

        # Sub-leg (c) — router-local-only spelling: entries spelled
        # router-local only with no canonicalization are dead at runtime and
        # must fail the guard loudly.
        spelling_findings = _recovery_divergence_findings(
            _mutant_router_local_only
        )
        self.assertTrue(
            spelling_findings,
            "guard blind spot: a router-local-only exemption entry matched "
            "without canonicalization is dead at runtime (deps.py matches "
            "the mounted /api/auth/... request path) and must fail the guard "
            "loudly",
        )
        self.assertTrue(
            any("/api/auth/change-password" in f for f in spelling_findings)
            and any("/api/auth/login" in f for f in spelling_findings),
            "the spelling finding must be actionable: name the mounted "
            f"runtime spellings that went dead — got {spelling_findings}",
        )

    def test_both_consumers_consume_the_shared_predicate(self):
        """Wiring leg: deps.py and maintenance.py must BOTH route their
        exemption decision through the shared predicate — an independent
        re-divergence (either file re-inlining a local set and dropping the
        predicate) fails this source-level check loudly."""
        from app.api import deps  # noqa: PLC0415 — post-fix import by design
        from app.middleware import maintenance  # noqa: PLC0415

        for label, module in (
            ("backend/app/api/deps.py", deps),
            ("backend/app/middleware/maintenance.py", maintenance),
        ):
            source = Path(module.__file__).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "app.security.recovery_routes"
                and any(alias.name == "is_recovery_exempt" for alias in node.names)
                for node in ast.walk(tree)
            )
            called = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "is_recovery_exempt"
                for node in ast.walk(tree)
            )
            self.assertTrue(
                imported and called,
                f"{label} no longer consumes the shared predicate: it must "
                "import AND call "
                "app.security.recovery_routes.is_recovery_exempt for its "
                "exemption decision (issue #1058) — re-inlining a local "
                "exemption set re-opens the exact divergence this guard "
                "exists to catch",
            )


if __name__ == "__main__":
    unittest.main()
