"""Acceptance checks AC1-AC3 (issue #1171, collated trace
1171-900-946-947-residuals-hygiene-sweep): ``redact_url`` must mask a
username-only userinfo and redact ``password=`` / ``username=`` query
parameters, and neither credential shape may reach a default-level boot
INFO channel.

Root cause (04-root-cause.md RC1): the structured rebuild path is gated on
``parsed.password is not None`` and the regex fallback requires a colon
inside the userinfo; neither path inspects ``parsed.query``. Consumers
(redis-py, via limiter.py:189-192 and embeddings.py:342-345) honor both
shapes as live credentials.

Frozen Phase 2.5 checks (arm's-length author; no fix code existed when
these were written):

- AC1/AC2 — marker-probe unit checks on the real helper.
  RED at base, GREEN after the fix.
- AC3 — channel probes drive the REAL production loggers with the
  capture_records pattern from
  tests/test_982_1131_c6_limiter_uri_redaction.py:53-77, plus the #1169
  pin-test flip contract (test_982_1131_guard_family_b_names.py:2526
  currently ASSERTS the username-only leak exists; the fix must flip it
  to assert NO leak). RED at base, GREEN after the fix.
- Preserving legs pin the already-correct shapes (password-with-colon
  masked, no-credentials passthrough, sentinel/multi-host forms),
  mirroring tests/test_rate_limiting.py::TestRedisUrlRedaction which must
  keep passing. PRESERVING — GREEN at base, must stay GREEN after the fix.

Standalone twin: .agents/issue-traces/1171-900-946-947-residuals-
hygiene-sweep/repro/check_redact_shapes.py drives the same shapes without
pytest for cheap replay.
"""

import asyncio
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.limiter import WhitelistLimiter, build_limiter
from app.utils.secrets import redact_url

# The production limiter logger is named "rate_limit" (limiter.py:16), NOT
# a descendant of "app.limiter" — probing a descendant would be vacuous.
LIMITER_LOGGER = "rate_limit"
EMBEDDINGS_LOGGER = "app.services.embeddings"

MARKER_USER = "TOKENuser1171"
MARKER_QUERY = "QUERYsecret1171"


class _ListHandler(logging.Handler):
    def __init__(self, level: int):
        super().__init__(level=level)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_records(logger_name: str, level: int = logging.INFO):
    """Capture LogRecords of one REAL logger (test_982_1131_c6 pattern)."""
    lg = logging.getLogger(logger_name)
    handler = _ListHandler(level)
    previous_level = lg.level
    lg.setLevel(level)
    lg.addHandler(handler)
    try:
        yield handler
    finally:
        lg.removeHandler(handler)
        lg.setLevel(previous_level)


# ---------------------------------------------------------------------------
# AC1 — username-only userinfo must be masked
# ---------------------------------------------------------------------------


def test_ac1_username_only_userinfo_masked():
    """AC1 (#1171): a bare-username userinfo (no colon) is a live
    credential for redis-py and must be masked. RED at base, GREEN after
    the fix.

    Plan-critic round 1 (amend wave): exact-output assertions added — the
    username-only arm must mask IN PLACE (preserve host list, ports, path,
    and every non-credential byte), never rebuild lossily.
    """
    single_host = f"redis://{MARKER_USER}@host.invalid:6379/0"
    out = redact_url(single_host)
    assert MARKER_USER not in out, (
        f"AC1-LEAK: redact_url returned a username-only userinfo verbatim: "
        f"{single_host!r} -> {out!r}"
    )
    assert out.startswith("redis://"), f"AC1: scheme lost: {out!r}"
    assert "host.invalid" in out, f"AC1: host lost: {out!r}"
    assert out == "redis://***@host.invalid:6379/0", (
        f"AC1: username-only masking must replace the username with *** and "
        f"preserve every other byte: {out!r}"
    )

    # Multi-host sentinel form exercises the REGEX fallback path (the
    # structured rebuild is excluded for comma netlocs) — the fallback's
    # colon requirement (secrets.py:17) is the second half of RC1.
    sentinel = (
        f"redis+sentinel://{MARKER_USER}@h1:26379,h2:26379/mymaster/0"
    )
    out = redact_url(sentinel)
    assert MARKER_USER not in out, (
        f"AC1-LEAK: redact_url returned a username-only userinfo verbatim "
        f"on the regex-fallback (multi-host sentinel) path: {sentinel!r} "
        f"-> {out!r}"
    )
    assert out == (
        "redis+sentinel://***@h1:26379,h2:26379/mymaster/0"
    ), (
        f"AC1: sentinel host list must survive username masking byte-for-byte"
        f" except the username itself: {out!r}"
    )


def test_ac1_malformed_credential_shapes_fail_closed():
    """AC1 amend (#1171 ABSORB comment, plan-critic round 1): the malformed
    credential shapes recorded into the issue by the second swarm-pr-review
    round of PR #1169 must never render verbatim. RED at base, GREEN after
    the fix.

    - ``redis://:sup3rs3cretpw`` (password-shaped token with no ``@host``):
      urlsplit yields hostname None (netloc ``:token``), so the structured
      path is skipped and the fallback regex requires ``@`` — verbatim leak
      on the boot INFO channel today.
    - ``redis://:Ab3/xYz9+Q@redis:6379/0`` (UNESCAPED ``/`` inside the
      password): urlsplit truncates the netloc at the ``/``
      (netloc ``:Ab3``), hostname None, port-cast raises — verbatim leak.
    """
    no_host = "redis://:sup3rs3cretpw1171"
    out = redact_url(no_host)
    assert "sup3rs3cretpw1171" not in out, (
        f"AC1-LEAK: redact_url returned a no-@host password-shaped netloc "
        f"verbatim: {no_host!r} -> {out!r}"
    )
    assert out == "redis://:***", (
        f"AC1: the no-@host credential token must be masked fail-closed: "
        f"{out!r}"
    )

    unescaped = "redis://:Ab3/xYz9+Q@redis:6379/0"
    out = redact_url(unescaped)
    assert "Ab3/xYz9+Q" not in out, (
        f"AC1-LEAK: redact_url returned an unescaped-/ password verbatim "
        f"(urlsplit truncates the netloc at the slash): {unescaped!r} -> "
        f"{out!r}"
    )
    assert out == "redis://:***@redis:6379/0", (
        f"AC1: the unescaped-/ password must be masked while the host/port "
        f"path survives: {out!r}"
    )

    # A digits-only token after ':' is a PORT, not a credential — it must
    # stay verbatim (fail-closed must not over-mask ports).
    assert redact_url("redis://:6379") == "redis://:6379", (
        "AC1: a port-only netloc is not a credential and must be preserved"
    )

    # Implementation-review round 1: URLs merely CONTAINING "@" later in the
    # query or path are credential-free and must pass through unchanged —
    # the pair regex's password class is bounded at "/", and the widened
    # empty-username arm requires ":" immediately after "://" and stops at
    # "?" and "#".
    assert redact_url("redis://host:6379/0?email=a@b") == (
        "redis://host:6379/0?email=a@b"
    ), "AC1: an @ inside an unrelated query value is not a credential"
    assert redact_url("redis://host:6379/path@x") == (
        "redis://host:6379/path@x"
    ), "AC1: an @ inside an unrelated path segment is not a credential"
    # Implementation-review round 2: the same invariant with NO path
    # separator before the query/fragment (the pair classes are bounded at
    # "?"/"#" too), and the ambiguous-slash arm's port disambiguator.
    assert redact_url("redis://host:6379?email=a@b") == (
        "redis://host:6379?email=a@b"
    ), "AC1: an @ inside a query value with no path separator is not a credential"
    assert redact_url("redis://host:6379#f@x") == (
        "redis://host:6379#f@x"
    ), "AC1: an @ inside a fragment is not a credential"
    assert redact_url("redis://:9000/path@host") == (
        "redis://:9000/path@host"
    ), "AC1: a digits-only token before a slash is a port, not a credential"


def test_ac1_nonempty_username_slash_password_masked():
    """AC1 amend (implementation-review round 2): a NON-EMPTY username with
    an unescaped-``/`` password is the mirror of the frozen empty-username
    shape — urlsplit truncates the netloc at the slash so only the
    ambiguous-slash fallback can mask it. RED at the round-1-fix tree,
    GREEN after the round-2 fix.
    """
    out = redact_url("redis://user:Ab3/xYz9+Q@redis:6379/0")
    assert "Ab3/xYz9+Q" not in out, (
        f"AC1-LEAK: redact_url returned a non-empty-username unescaped-/ "
        f"password verbatim: {out!r}"
    )
    assert out == "redis://user:***@redis:6379/0", (
        f"AC1: the username survives, the slash-password is masked: {out!r}"
    )


def test_ac1_malformed_shapes_off_the_boot_info_channel():
    """AC1 amend (plan-critic round 2): the malformed credential shapes must
    never reach the rate_limit boot INFO line either. The wired-to-Redis INFO
    fires BEFORE limiter construction, so both shapes are probed with the
    constructor's failure tolerated (raise or construct — the leak channel is
    the log line). RED at base, GREEN after the fix."""
    shapes = [
        ("redis://:sup3rs3cretpw1171", "sup3rs3cretpw1171"),
        ("redis://:Ab3/xYz9+Q@redis:6379/0", "Ab3/xYz9+Q"),
    ]
    for url, marker in shapes:
        with capture_records(LIMITER_LOGGER, logging.INFO) as captured:
            try:
                build_limiter(url)
            except Exception:  # noqa: BLE001 — constructor may legitimately fail
                pass
        wired = [
            r for r in captured.records if "wired to Redis" in r.getMessage()
        ]
        assert wired, (
            "sanity: the boot INFO line fired on rate_limit for "
            f"{url!r} (otherwise this probe is vacuous)"
        )
        leaked = [
            r.getMessage() for r in captured.records if marker in r.getMessage()
        ]
        assert leaked == [], (
            f"AC3-LEAK: rate_limit boot INFO carries a malformed credential "
            f"marker for {url!r}: {leaked}"
        )


# ---------------------------------------------------------------------------
# AC2 — password= / username= query parameters must be redacted
# ---------------------------------------------------------------------------


def test_ac2_query_param_credentials_redacted():
    """AC2 (#1171): redis-py honors ``password=`` / ``username=`` query
    parameters as credentials; their values must be redacted. RED at
    base, GREEN after the fix.

    Plan-critic round 1 (amend wave): exact-output assertions added for
    encoded keys (redis decodes ``pass%77ord`` to ``password``), duplicate
    parameters, unrelated-parameter/order preservation, and fragment
    preservation — the redaction must be raw-span (touch ONLY the
    credential values), never a re-serialization of the query.
    """
    cases = [
        (
            f"redis://host.invalid:6379/0?password={MARKER_QUERY}",
            MARKER_QUERY,
            "password=",
        ),
        (
            f"redis://host.invalid:6379/0?username={MARKER_USER}",
            MARKER_USER,
            "username=",
        ),
        # Combined shape: the userinfo password is already masked at base;
        # the query password must ALSO be masked once the fix lands.
        (
            f"redis://app:p%40ss@host.invalid:6379/0?password={MARKER_QUERY}",
            MARKER_QUERY,
            "password=",
        ),
    ]
    for url, marker, param_name in cases:
        out = redact_url(url)
        assert marker not in out, (
            f"AC2-LEAK: redact_url passed a query-string credential "
            f"through verbatim: {url!r} -> {out!r}"
        )
        assert param_name in out, (
            f"AC2: the parameter name itself must be retained with its "
            f"value redacted: {url!r} -> {out!r}"
        )
        assert "host.invalid" in out, f"AC2: host lost: {out!r}"

    # Exact outputs: value -> ***, key spelling and all other bytes intact.
    assert redact_url(
        f"redis://host.invalid:6379/0?password={MARKER_QUERY}"
    ) == "redis://host.invalid:6379/0?password=***"
    assert redact_url(
        f"redis://host.invalid:6379/0?username={MARKER_USER}"
    ) == "redis://host.invalid:6379/0?username=***"
    # Encoded key names: redis decodes before matching, so the redaction
    # must classify on the DECODED name while leaving the raw key bytes.
    assert redact_url(
        f"redis://host.invalid:6379/0?pass%77ord={MARKER_QUERY}"
    ) == "redis://host.invalid:6379/0?pass%77ord=***"
    assert redact_url(
        f"redis://host.invalid:6379/0?user%6eame={MARKER_USER}"
    ) == "redis://host.invalid:6379/0?user%6eame=***"
    # Duplicates: every credential occurrence is redacted.
    assert redact_url(
        f"redis://host.invalid:6379/0?password={MARKER_QUERY}&x=1"
        f"&password={MARKER_QUERY}"
    ) == "redis://host.invalid:6379/0?password=***&x=1&password=***"
    # Unrelated parameters keep their values and order.
    assert redact_url(
        f"redis://host.invalid:6379/0?db=0&password={MARKER_QUERY}"
        f"&socket_timeout=1.5"
    ) == "redis://host.invalid:6379/0?db=0&password=***&socket_timeout=1.5"
    # The fragment is not part of the query and must survive untouched.
    assert redact_url(
        f"redis://host.invalid:6379/0?password={MARKER_QUERY}#frag"
    ) == "redis://host.invalid:6379/0?password=***#frag"
    # Implementation-review round 3: a "?" INSIDE the fragment is fragment
    # text, not a query start (urlparse gives .query == "" here, and redis-py
    # honors only the real query) — the whole URL must pass through
    # unchanged.
    assert redact_url("redis://host/#section?password=visible") == (
        "redis://host/#section?password=visible"
    ), "AC2: a ? inside the fragment is not a query parameter"
    # A query on a multi-host sentinel URL (fallback path) is redacted too.
    assert redact_url(
        f"redis+sentinel://:pw@h1:26379,h2:26379/mymaster/0"
        f"?password={MARKER_QUERY}"
    ) == (
        "redis+sentinel://:***@h1:26379,h2:26379/mymaster/0?password=***"
    )


# ---------------------------------------------------------------------------
# AC3 — boot INFO channels must not carry either credential shape
# ---------------------------------------------------------------------------


def test_ac3_limiter_boot_info_no_credential_leak():
    """AC3 (#1171): the boot INFO line at limiter.py:189-192 (real
    'rate_limit' logger) renders redact_url output; neither credential
    shape may appear at INFO. RED at base, GREEN after the fix."""
    shapes = [
        (f"redis://{MARKER_USER}@redis-1171.invalid:6379/0", MARKER_USER),
        (
            f"redis://redis-1171.invalid:6379/0?password={MARKER_QUERY}",
            MARKER_QUERY,
        ),
    ]
    for url, marker in shapes:
        with capture_records(LIMITER_LOGGER, logging.INFO) as captured:
            lim = build_limiter(url)
        assert isinstance(lim, WhitelistLimiter), (
            "sanity: the limiter constructed for a registered redis:// URI"
        )
        wired = [
            r for r in captured.records if "wired to Redis" in r.getMessage()
        ]
        assert wired, (
            "sanity: the wired-to-Redis INFO line fired on rate_limit "
            "(otherwise this probe is vacuous)"
        )
        leaked = [
            r.getMessage() for r in captured.records if marker in r.getMessage()
        ]
        assert leaked == [], (
            f"AC3-LEAK: rate_limit boot INFO carries a credential marker "
            f"for {url!r}: {leaked}"
        )


def test_ac3_embeddings_boot_info_no_credential_leak(monkeypatch):
    """AC3 (#1171): the boot INFO line at embeddings.py:342-345 (real
    'app.services.embeddings' logger) renders redact_url output; neither
    credential shape may appear at INFO. RED at base, GREEN after the
    fix."""
    from app.config import settings as live_settings
    from app.services.embeddings import EmbeddingService

    shapes = [
        (f"redis://{MARKER_USER}@host.invalid:6379/0", MARKER_USER),
        (
            f"redis://host.invalid:6379/0?password={MARKER_QUERY}",
            MARKER_QUERY,
        ),
    ]
    for url, marker in shapes:
        monkeypatch.setattr(live_settings, "redis_url", url)
        fake_client = MagicMock()
        fake_client.ping.return_value = True
        with patch("app.services.embeddings.redis") as fake_redis:
            fake_redis.from_url.return_value = fake_client
            with patch("app.services.embeddings.assert_url_safe"):
                with capture_records(EMBEDDINGS_LOGGER, logging.INFO) as cap:
                    service = EmbeddingService()
        try:
            connected = [
                r
                for r in cap.records
                if "L2 cache connected" in r.getMessage()
            ]
            assert connected, (
                "sanity: the Embedding Redis L2 cache connected INFO line "
                "fired (otherwise this probe is vacuous)"
            )
            leaked = [
                r.getMessage() for r in cap.records if marker in r.getMessage()
            ]
            assert leaked == [], (
                f"AC3-LEAK: embeddings boot INFO carries a credential "
                f"marker for {url!r}: {leaked}"
            )
        finally:
            asyncio.run(service._client.aclose())


def test_ac3_pin_test_flipped_to_no_leak():
    """AC3 (#1171): the #1169 pin test
    (test_limiter_info_log_channel_pins_known_1171_leak) pins the known
    username-only leak with a TRUTHY leak assertion; the fix must flip it
    to assert NO leak. RED at base, GREEN after the fix (the flip lands
    with the redact_url hardening in the same change).

    Plan-critic round 1 (amend wave): the flip contract is strengthened —
    deleting the assertion or leaving a vacuous form must FAIL. The source
    must (a) still compute the `leaked` list from captured records, and
    (b) carry an explicit NEGATIVE leak assertion (`assert leaked == []`
    or `assert not leaked`), and the truthy `assert leaked,` form must be
    gone. Additionally the flipped pin test is EXECUTED via a subprocess
    pytest run and must PASS (proving the flipped assertion actually holds
    on the hardened helper).
    """
    import inspect
    import subprocess
    import sys as _sys

    import test_982_1131_guard_family_b_names as guard_mod

    src = inspect.getsource(
        guard_mod.test_limiter_info_log_channel_pins_known_1171_leak
    )
    assert "build_limiter" in src, (
        "sanity: the pin test still drives build_limiter"
    )
    assert "marker" in src, (
        "sanity: the pin test still probes with a credential marker"
    )
    assert re.search(r"leaked\s*=\s*\[", src), (
        "AC3-PIN-GUTTED: the pin test no longer computes the leaked list "
        "from the captured records; the flip must keep the probe and "
        "invert the assertion, not delete the check"
    )
    # The leak-pinning form is exactly `assert leaked,` (truthy assert).
    # A no-leak flip (`assert not leaked`, `assert leaked == []`, ...)
    # does not match; the base truthy form does.
    truthy_leak_assert = re.search(r"assert\s+leaked\s*(?:,|$)", src, re.M)
    assert truthy_leak_assert is None, (
        "AC3-PIN-NOT-FLIPPED: test_limiter_info_log_channel_pins_known_"
        "1171_leak still ASSERTS the username-only leak exists; flip it to "
        "assert NO leak alongside the redact_url hardening"
    )
    negative_leak_assert = re.search(
        r"assert\s+(?:not\s+leaked|leaked\s*==\s*\[\])", src
    )
    assert negative_leak_assert, (
        "AC3-PIN-NOT-FLIPPED: the pin test no longer carries the truthy "
        "leak assert but has no explicit negative leak assertion either "
        "(a deleted or vacuous assertion is not a flip)"
    )
    # Execute the flipped pin test itself: it must PASS on the hardened
    # helper (subprocess so its caplog isolation is the real pytest one).
    # cwd is backend/ (parents[1]) so the tests/... node id resolves.
    proc = subprocess.run(
        [
            _sys.executable,
            "-m",
            "pytest",
            "tests/test_982_1131_guard_family_b_names.py::"
            "test_limiter_info_log_channel_pins_known_1171_leak",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"AC3-PIN-EXEC-FAILED: the flipped pin test did not pass when "
        f"executed: {proc.stdout[-800:]} {proc.stderr[-400:]}"
    )


# ---------------------------------------------------------------------------
# Preserving legs — the already-correct shapes must not change
# ---------------------------------------------------------------------------


def test_preserving_userinfo_password_masked():
    """PRESERVING: the user:password@ shapes that already mask correctly
    (asserted by tests/test_rate_limiting.py:1118-1155) must keep their
    exact masked output. GREEN at base, must stay GREEN after the fix."""
    assert redact_url("redis://:s3cret@redis:6379/2") == (
        "redis://:***@redis:6379/2"
    )
    assert redact_url("redis://app:p%40ss@redis:6379/0") == (
        "redis://app:***@redis:6379/0"
    )
    assert redact_url("rediss://:pw@tls-redis:6379/1") == (
        "rediss://:***@tls-redis:6379/1"
    )
    assert redact_url("redis+sentinel://:pw@host:26379/mymaster/0") == (
        "redis+sentinel://:***@host:26379/mymaster/0"
    )
    assert redact_url(
        "redis+sentinel://app:pw@h1:26379,h2:26379/mymaster/0"
    ) == "redis+sentinel://app:***@h1:26379,h2:26379/mymaster/0"


def test_preserving_no_credential_passthrough_and_multihost():
    """PRESERVING: URLs without credentials pass through unchanged and
    multi-host sentinel URLs neither crash nor drop host entries.
    GREEN at base, must stay GREEN after the fix.

    Plan-critic round 1 (amend wave): the non-credential boundary cases —
    empty userinfo (``@host`` with no username), port-only netlocs, and
    the pre-existing lossy malformed-port rebuild — are pinned so the new
    fail-closed arms cannot over-mask (the first two stay byte-identical;
    the malformed-port lossy rebuild is pinned AS-IS to document that the
    username-only fix introduces no NEW lossy behavior — it is a
    disclosed pre-existing residual owned by this assertion).
    """
    assert redact_url("redis://redis.invalid:6379/0") == (
        "redis://redis.invalid:6379/0"
    )
    assert redact_url("memory://") == "memory://"
    assert redact_url("") == ""
    assert redact_url(
        "redis+sentinel://:pw@h1:26379,h2:26379/mymaster/0"
    ) == "redis+sentinel://:***@h1:26379,h2:26379/mymaster/0"
    # Empty userinfo is NOT a credential: unchanged.
    assert redact_url("redis://@host.invalid:6379/0") == (
        "redis://@host.invalid:6379/0"
    )
    # Port-only netloc: the digits-only token is a port, unchanged.
    assert redact_url("redis://:6379") == "redis://:6379"
    # Pre-existing lossy malformed-port rebuild for user:pass@ shapes:
    # pinned as-is (the username-only arm must NOT route through this
    # lossy rebuild — see AC1's exact-output assertions).
    assert redact_url("redis://:pw@host:notaport/0") == "redis://:***@host/0"
