"""Shared recovery-route exemption decision (issue #1058).

The forced-password-change recovery exit is the one surface TWO gates must
keep open in agreement: the ``must_change_password`` dependency gate
(app/api/deps.py) and the maintenance middleware's write-block exemption
(app/middleware/maintenance.py). PR #1057 proved the two allowlists could
silently disagree (a flagged user authenticates during a maintenance window
but has no working exit), and that the AST guard pinning them answered only
for the literal/POST authoring shape.

This module is the SINGLE decision point both gates consume: one canonical,
method-aware table of recovery exemptions plus one canonicalizing predicate.
``is_recovery_exempt(method, path)`` owns the spelling rules (router-local
``/auth/...`` and mounted ``/api/auth/...`` both canonicalize to the mounted
truth; trailing slashes normalize) so no call-site spelling stays
load-bearing. Maintenance additionally keeps its own ops-only POST set
(admin toggle, token refresh, logout) — those are operational, not recovery
surfaces, and stay in the middleware where they are used.

Guard: ``backend/tests/test_recovery_routes_guard.py`` pins the predicate
answers for today's surfaces, fails loudly on the three divergence classes
PR #1057 left open (post-assignment/missed-shape growth, method blindness,
router-local-only spelling), and wiring-checks that both consumers route
their decision through this predicate.
"""

from __future__ import annotations

# Canonical (mounted) recovery-exempt surfaces -> allowed methods. The table
# IS the decision: growing it is a conscious classification decision that the
# guard's pinned matrix and the classification registry in
# backend/tests/test_exempt_paths_guard.py both attest.
_RECOVERY_EXEMPT: dict[str, frozenset[str]] = {
    # The forced-change exit itself (POST only — the route is a POST).
    "/api/auth/change-password": frozenset({"POST"}),
    # Login stays reachable so a flagged user can authenticate at all.
    "/api/auth/login": frozenset({"POST"}),
    # GET /auth/me is the SPA's rehydration read of the caller's own identity
    # (me-read in the old deps.py sets): read-only, GET only — every other
    # method on /auth/me stays blocked so the forced change stays the only
    # profile/password exit.
    "/api/auth/me": frozenset({"GET"}),
}


def is_recovery_exempt(method: str, path: str) -> bool:
    """Is ``(method, path)`` a recovery-exempt surface for flagged users?

    Canonicalizes ``path`` (trailing slash; router-local ``/auth/...`` to the
    mounted ``/api/auth/...`` runtime spelling) and answers method-aware from
    ``_RECOVERY_EXEMPT``. Unknown paths and disallowed methods answer False.
    """
    canonical = (path or "/").rstrip("/") or "/"
    if not canonical.startswith("/api/") and canonical.startswith("/auth/"):
        canonical = "/api" + canonical
    allowed = _RECOVERY_EXEMPT.get(canonical)
    return allowed is not None and method.upper() in allowed
