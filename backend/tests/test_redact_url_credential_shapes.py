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
import time
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
    # Final-critic round 1: redis-py 8.x routes ssl_password to SSLConnection
    # as the TLS private-key passphrase (parse_url probe) — a live credential
    # that must be redacted; any other ...password key is masked fail-closed
    # by the suffix rule.
    assert redact_url("redis://host/0?ssl_password=KEYPW1") == (
        "redis://host/0?ssl_password=***"
    ), "AC2: ssl_password is a live redis-py TLS passphrase credential"
    assert redact_url("redis://host/0?rediss_password=KEYPW1") == (
        "redis://host/0?rediss_password=***"
    ), "AC2: any ...password-suffixed key is masked fail-closed"
    # Keys that merely CONTAIN "password" but do not end with it stay
    # untouched: password_file is a PATH redis-py does not accept as a query
    # key at all, and masking paths would corrupt a credential-free value.
    assert redact_url("redis://host/0?password_file=%2Fetc%2Fpw") == (
        "redis://host/0?password_file=%2Fetc%2Fpw"
    ), "AC2: password_file is a path, not a password value — preserved"
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
    empty userinfo (``@host`` with no username) and port-only netlocs — are
    pinned so the new fail-closed arms cannot over-mask. The malformed-port
    lossy rebuild was FIXED in the feedback round (raw-netloc-tail rebuild
    preserves the port text); the fidelity pin below replaces the old
    lossy pin.
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
    # Malformed-port fidelity (feedback FB-003/ST-003 fix): the structured
    # rebuild now re-renders from the RAW netloc tail, so the port TEXT is
    # preserved (previously dropped) and IPv6 brackets survive.
    assert redact_url("redis://:pw@host:notaport/0") == (
        "redis://:***@host:notaport/0"
    )

def test_ac1_userinfo_at_sign_fully_masked():
    """AC1 amend (feedback FB-003, PR-review ST-001/ST-002/AS-001): userinfo
    credentials whose token carries a raw ``@`` are masked through the LAST
    ``@`` (urlsplit rpartition semantics — the same parse redis-py uses), on
    both the structured single-host path and the multi-host/username-only
    fallback paths. RED before the fix (tails ``ss``/``ken`` leaked), GREEN
    after.
    """
    assert redact_url("redis://svc:p@ss@redis:6379/0") == (
        "redis://svc:***@redis:6379/0"
    ), "AC1: single-host pair password containing @ must mask fully"
    assert redact_url(
        "redis+sentinel://svc:p@ss@h1:26379,h2:26379/mymaster/0"
    ) == "redis+sentinel://svc:***@h1:26379,h2:26379/mymaster/0", (
        "AC1: multi-host sentinel pair password containing @ must mask fully"
    )
    assert redact_url("redis://to@ken@redis:6379/0") == (
        "redis://***@redis:6379/0"
    ), "AC1: username-only token containing @ must mask fully"
    assert redact_url(
        "redis+sentinel://to@ken@h1:26379,h2:26379/0"
    ) == "redis+sentinel://***@h1:26379,h2:26379/0", (
        "AC1: multi-host username-only token containing @ must mask fully"
    )
    # Query/path @ characters still cannot feed the arms.
    assert redact_url("redis://to@ken@redis:6379/0?email=a@b") == (
        "redis://***@redis:6379/0?email=a@b"
    ), "AC1: a query @ stays untouched while the userinfo token masks fully"


def test_ac1_ipv6_and_port_fidelity():
    """AC1 amend (feedback ST-003 + lossy-port fix): the structured rebuild
    re-renders from the RAW netloc tail, so IPv6 brackets survive and a
    malformed port's TEXT is preserved (the credential is still masked).
    The bracket-drop and port-drop outputs were the pre-fix behavior.
    """
    assert redact_url("rediss://user:pass@[::1]:6379/0") == (
        "rediss://user:***@[::1]:6379/0"
    ), "AC1: IPv6 brackets must survive pair masking"
    assert redact_url("redis://user:pass@[2001:db8::1]/0") == (
        "redis://user:***@[2001:db8::1]/0"
    ), "AC1: full IPv6 host text must survive pair masking"
    assert redact_url("redis://user:pass@[::1]:6379/0?x=1") == (
        "redis://user:***@[::1]:6379/0?x=1"
    ), "AC1: bracket fidelity holds with a query attached"


def test_ac2_bracket_malformed_fail_closed():
    """AC2 amend (feedback FB-008/PO-001): urlsplit raises ValueError on
    bracket-malformed IPv6 netlocs; the helper must NEVER raise (logging-path
    contract) and must mask the credential-shaped span fail-closed instead of
    echoing it. RED at the pre-fix tree (raw ValueError), GREEN after.
    """
    assert redact_url("redis://:pw@[::1") == "redis://:***@[::1", (
        "AC2: bracket-malformed leading-colon netloc must mask fail-closed"
    )
    assert redact_url("redis://:pw@[zz]") == "redis://:***@[zz]", (
        "AC2: bracket-malformed non-IPv6 token must mask fail-closed"
    )
    assert redact_url("redis://user:pass@[::1") == "redis://user:***@[::1", (
        "AC2: bracket-malformed pair netloc must mask fail-closed"
    )


def test_ac2_fail_closed_tail_preserved():
    """AC2 amend (feedback FB-005/ST-004/CS-001): the fail-closed
    ``:<token>`` arm masks the token but PRESERVES the path/query that
    follows the netloc (pre-fix behavior dropped everything after the
    token). RED at the pre-fix tree (tail dropped), GREEN after.
    """
    assert redact_url("redis://:sup3rs3cretpw1171/0?db=1") == (
        "redis://:***/0?db=1"
    ), "AC2: fail-closed masking must preserve the post-netloc tail"
    assert redact_url("redis://:sup3rs3cretpw1171") == "redis://:***", (
        "AC2: the tail-less shape stays exact"
    )


def test_ac2_qmark_hash_password_documented():
    """AC2 amend (feedback FB-004/ST-005, Stage B re-gate): a password span
    crossing an unescaped ``?``/``#`` is masked FAIL-CLOSED — the issue's
    malformed-shape demand (#1171) — with the URL tail after the delimiter
    preserved. Credential-free port+query shapes
    (``redis://host:6379?email=a@b``) stay untouched via the digits rule.
    Pins the behavior AND the function docstring's fail-closed-arm sentence
    so the pin and the contract cannot drift apart.
    """
    import app.utils.secrets as secrets_module

    assert redact_url("redis://user:hunter2?x@host:6379/0") == (
        "redis://user:***@host:6379/0"
    ), "AC2: the ?-in-password span must mask fail-closed, tail preserved"
    assert redact_url("redis://user:hunter2#x@host:6379/0") == (
        "redis://user:***@host:6379/0"
    ), "AC2: the #-in-password span must mask fail-closed, tail preserved"
    assert "password spans that cross" in secrets_module.redact_url.__doc__, (
        "AC2: the function docstring must carry the qmark/hash fail-closed "
        "sentence so the pin and the contract cannot drift apart"
    )


def test_ac1_consecutive_at_username_masked():
    """Stage B re-gate: consecutive ``@`` inside a username-only token.
    urlsplit's rpartition makes ``to@@ken`` the username (the empty token
    between the @s belongs to the userinfo), so the WHOLE userinfo must
    mask — the pre-fix arm stopped at the first empty token and leaked
    ``@ken``."""
    out = redact_url("redis://to@@ken@host/0")
    assert out == "redis://***@host/0", (
        f"AC1: consecutive-@ username must mask fully, got {out!r}"
    )


def test_ac2_degenerate_at_userinfo_passthrough():
    """An EMPTY single-``@`` userinfo carries no credential bytes (urlsplit
    yields username None) and must pass through byte-for-byte — the
    masking arms must not corrupt credential-free URLs. A DOUBLE ``@``
    (``redis://@@host``) is NOT degenerate: urlsplit's rpartition makes
    ``@`` a non-empty username, so it masks (Stage B F-003 edge)."""
    for url in ("redis://@host", "redis://@host/0"):
        out = redact_url(url)
        assert out == url, (
            f"AC2: empty-@ userinfo {url!r} must pass through, got {out!r}"
        )
    assert redact_url("redis://@@host") == "redis://***@host", (
        "AC2: double-@ userinfo (username '@') must mask"
    )


def test_ac2_multihost_qmark_query_preserved():
    """Stage B NEW-3: the qmark/hash arm must not reinterpret an
    already-masked multi-host userinfo — the host list and query of
    ``redis+sentinel://user:pw@h1,h2?email=a@b`` stay intact while the
    password masks."""
    out = redact_url("redis+sentinel://user:pw@h1,h2?email=a@b")
    assert out == "redis+sentinel://user:***@h1,h2?email=a@b", (
        f"AC2: multi-host + query-@ must preserve host list and query, "
        f"got {out!r}"
    )


def test_ac2_query_at_anchor_userinfo_masked():
    """Stage B NEW-2: a query value containing ``@`` must not consume the
    ``@`` the userinfo arms anchor on — the userinfo password masks even
    when the query credential is redacted first is NOT the order (userinfo
    arms run first)."""
    out = redact_url("redis://user:hunter2?password=x@host")
    assert "hunter2" not in out, (
        f"AC2: userinfo password must not survive a query-@ shape: {out!r}"
    )
    assert out == "redis://user:***@host", (
        f"AC2: query-@ shape must mask fail-closed, got {out!r}"
    )


def test_ac2_query_scan_linear_with_late_equals():
    """Stage B F-006 residual: a long '='-less FIRST parameter followed by
    a LATER parameter carrying '=' must stay linear — the segment scan
    must not backtrack across the long token."""
    url = f"redis://host:6379/0?{'k' * 100_000}&x=1"
    t0 = time.perf_counter()
    out = redact_url(url)
    elapsed = time.perf_counter() - t0
    assert out == url, "AC2: non-credential segments must pass through"
    assert elapsed < 0.5, (
        f"AC2: late-'=' parameter must stay off the quadratic path, "
        f"took {elapsed:.3f}s"
    )


def test_ac2_qmark_hash_pair_fail_closed():
    """#1171 scope (Stage B): a password span crossing ``?``/``#`` is
    masked fail-closed — the pre-delimiter ``user:token`` shape is
    credential-shaped — not echoed verbatim."""
    for url in ("redis://user:hunter2?x@host", "redis://user:hunter2#frag@host"):
        out = redact_url(url)
        assert out.startswith("redis://user:***@"), (
            f"AC2: qmark/hash password span must mask fail-closed, got {out!r}"
        )


def test_ac2_digits_port_qmark_passthrough():
    """The digits rule keeps credential-free URLs intact: an all-digits
    token before ``?`` is a port plus a query, not a password."""
    url = "redis://host:6379?email=a@b"
    out = redact_url(url)
    assert out == url, (
        f"AC2: port+query must pass through byte-for-byte, got {out!r}"
    )


def test_ac2_lessequals_guard_fragment():
    """F-006 residual (Stage B): the '='-absence fast-path guard must see
    the QUERY alone — a long '='-less query followed by a fragment that
    merely CONTAINS '=' must not defeat the guard into the quadratic scan."""
    long_query = "k" * 100_000
    url = f"redis://host:6379/0?{long_query}#x=y"
    t0 = time.perf_counter()
    out = redact_url(url)
    elapsed = time.perf_counter() - t0
    assert out == url, f"AC2: '='-less query + '='-fragment must pass through, got {out[:80]!r}"
    assert elapsed < 0.5, (
        f"AC2: '='-less query + '='-fragment must stay off the quadratic "
        f"path, took {elapsed:.3f}s"
    )


def test_ac2_qmark_cross_multi_at_masked():
    """Stage B NEW-4: a crossed password span with MULTIPLE inner ``@``s
    (``redis://user:secret?part@TAILSECRET@host``) masks WHOLE — urlsplit's
    netloc terminates at the first ``?``, so without the crossed-span arm
    the structured rebuild leaves ``TAILSECRET@host`` verbatim in the
    rendered query. The arm masks through the LAST ``@`` of the pre-``/``
    segment; the digits rule keeps ``redis://host:6379?email=a@b`` intact."""
    out = redact_url("redis://user:secret?part@TAILSECRET@host")
    assert "TAILSECRET" not in out and "part" not in out, (
        f"AC2: multi-@ crossed password must mask whole, got {out!r}"
    )
    assert out == "redis://user:***@host", (
        f"AC2: crossed span must mask through the last @, got {out!r}"
    )
    # Mixed delimiters inside the crossed span (?..# and #..?) mask whole too
    # (Stage B NEW-5), as do slash-crossed spans (Stage B NEW-6): the arm is
    # prefix-regex + last-@ search, delimiter-agnostic up to the URL's last @.
    for shape in (
        "redis://user:secret?part#TAILSECRET@host",
        "redis://user:secret#part?TAILSECRET@host",
        "redis://user:secret?part#TAILSECRET@[::1",
        "redis://user:secret/x@TAILSECRET@host",
        "redis://user:secret/a/b@TAILSECRET@host",
    ):
        out = redact_url(shape)
        assert "TAILSECRET" not in out and out.startswith("redis://user:***@"), (
            f"AC2: mixed/slash-delimiter crossed span must mask whole, got {out!r}"
        )
    assert redact_url("redis://host:6379?email=a@b") == (
        "redis://host:6379?email=a@b"
    ), "AC2: digits rule must keep port+query passthrough intact"
    assert redact_url("redis://user:123/secret@host") == (
        "redis://user:123/secret@host"
    ), "AC2: digits rule must keep the documented name:digits/path@ shape intact"
    # Stage B NEW-7: a later path/query @ must NOT be consumed as the
    # credential separator — the host/path/query tail survives.
    for shape in (
        "redis://user:secret/x@host/0?email=a@b",
        "redis://user:secret?part@host/0?email=a@b",
    ):
        out = redact_url(shape)
        assert out == "redis://user:***@host/0?email=a@b", (
            f"AC2: crossed span must end before host/path/query text, got {out!r}"
        )
    # Stage B F-006 dense-:// residual: an @-free dense-:// input stays off
    # the quadratic fallback scans entirely (the @-presence gate).
    dense = "redis://" + ("a://" * 20000)
    t0 = time.perf_counter()
    out = redact_url(dense)
    elapsed = time.perf_counter() - t0
    assert out == dense, "AC2: @-free dense-:// input must pass through"
    assert elapsed < 0.5, (
        f"AC2: dense-:// input must stay linear, took {elapsed:.3f}s"
    )


def test_ac2_orphaned_query_after_cross_masked():
    """Closeout review: a crossed-span/qmark mask can consume the original
    ``?`` together with a query ``@`` — the orphaned parameters after the
    final ``@`` must still get the credential-key redaction
    (``redis://host:bad?password=x@y&password=SECRET`` leaves
    ``&password=***``, never ``SECRET``)."""
    out = redact_url("redis://host:bad?password=x@y&password=SECRET")
    assert "SECRET" not in out, (
        f"AC2: orphaned query credential must be redacted, got {out!r}"
    )
    assert out == "redis://host:***@y&password=***", (
        f"AC2: orphaned-query sweep must mask the credential param, got {out!r}"
    )
    out = redact_url("redis://user:secret?part@x&password=SECRET")
    assert out == "redis://user:***@x&password=***", (
        f"AC2: qmark-cross orphaned query must be redacted, got {out!r}"
    )
    # A later path @ must not displace the sweep boundary (closeout NEW-10):
    out = redact_url("redis://host:bad?password=x@y&password=SECRET&email=a/b@c")
    assert out == "redis://host:***@y&password=***&email=a/b@c", (
        f"AC2: later path-@ must not bypass the orphan sweep, got {out!r}"
    )
    # Scope guard (closeout): the sweep runs ONLY when the original carried
    # a real query that the mask consumed — no-query URLs keep every
    # fragment/path byte.
    for shape in (
        "redis://host/0#a@b&password=c",
        "redis://host/a@b/password=1",
        "redis://host/0#note@x&username=bob",
    ):
        out = redact_url(shape)
        assert out == shape, (
            f"AC2: no-query URL must stay byte-preserved, got {out!r}"
        )
    assert redact_url("redis://u:p@host/0#password=keep") == (
        "redis://u:***@host/0#password=keep"
    ), "AC2: a no-query URL's fragment must stay byte-preserved"
    # A later "?" inside a QUERY VALUE must not defeat the consumed-query
    # detection (closeout NEW-11): the delimiter is tracked by "?" COUNT,
    # and once consumed the sweep replaces the normal scan so a surviving
    # value-"?" cannot strand earlier credential parameters.
    out = redact_url("redis://host:bad?password=x@y&password=SECRET&next=?foo")
    assert "SECRET" not in out, (
        f"AC2: value-'?' must not bypass the orphan sweep, got {out!r}"
    )
    assert out == "redis://host:***@y&password=***&next=?foo", (
        f"AC2: value-'?' sweep must preserve non-credential params, got {out!r}"
    )
    assert redact_url("redis://user:pass?a=?b&password=SECRET2") == (
        "redis://user:pass?a=?b&password=***"
    ), "AC2: a live query with a value-'?' still gets the normal scan"
    # A query VALUE embedding a full URL (nested "://") must not redirect
    # the crossed-span classification away from the outer query (closeout
    # NEW-12): the outer credential param masks, the URL-valued param stays.
    out = redact_url("redis://host/0?password=SECRET&next=http://user:pass/x?part@h")
    assert "SECRET" not in out, (
        f"AC2: nested-URL query value must not bypass the query scan, got {out!r}"
    )
    assert out == (
        "redis://host/0?password=***&next=http://user:pass/x?part@h"
    ), f"AC2: URL-valued query param must stay verbatim, got {out!r}"
    # The fallback userinfo arms carry the same top-level guard (closeout
    # NEW-13): a nested "user:pass?part@h" inside a URL-valued query param
    # must not feed the qmark arm (which would skew the delimiter count and
    # strand the outer credential).
    out = redact_url("redis://host/0?password=SECRET&next=http://user:pass?part@h")
    assert "SECRET" not in out, (
        f"AC2: nested qmark value must not bypass the query scan, got {out!r}"
    )
    assert out == (
        "redis://host/0?password=***&next=http://user:pass?part@h"
    ), f"AC2: nested qmark URL value must stay verbatim, got {out!r}"
    # A nested URL in a NO-query FRAGMENT must also stay verbatim (closeout
    # NEW-14): the outer-authority guard bounds userinfo matching to the
    # scheme's authority, before the first /, ? or #.
    shape = "redis://host/0#next=http://user:pass/x@h"
    out = redact_url(shape)
    assert out == shape, (
        f"AC2: nested URL in a fragment must stay byte-preserved, got {out!r}"
    )
    # The leading-colon arm carries the same outer-authority boundary
    # (closeout NEW-15): a nested "redis://:token" inside a fragment or
    # query value is not this URL's userinfo.
    for shape in (
        "redis://host/0#next=redis://:notaport",
        "redis://host/0?next=redis://:notaport",
    ):
        out = redact_url(shape)
        assert out == shape, (
            f"AC2: nested leading-colon URL must stay byte-preserved, got {out!r}"
        )
    # SCOPE (documented in the module docstring): an EMBEDDED url is opaque
    # text — a different url, not this one's userinfo — while the outer
    # url's own credential parameter is still masked.
    assert redact_url("redis://host/0?next=http://user:pass@h") == (
        "redis://host/0?next=http://user:pass@h"
    ), "AC2: an embedded url in a query value is opaque text"
    assert redact_url("redis://host/0?password=OUTER&next=http://user:pass@h") == (
        "redis://host/0?password=***&next=http://user:pass@h"
    ), "AC2: the outer url's own credential parameter is still masked"
    # The CROSSED arm must not consume an embedded url either (closeout
    # NEW-16): the "@"s of the embedded url belong to it, so the crossed
    # span has no separator of its own — the host:port prefix is left as
    # it is (the existing host:port contract) and the embedded url's bytes
    # survive byte-for-byte.
    assert redact_url("redis://host:bad?next=http://user:pass@h") == (
        "redis://host:bad?next=http://user:pass@h"
    ), "AC2: the crossed arm must not consume an embedded url"
    # The structured rebuild must SPLICE the netloc, not re-assemble the url:
    # urlunsplit drops an empty "?"/"#" delimiter (closeout NEW-17).
    for shape, expected in (
        ("redis://u:p@host?", "redis://u:***@host?"),
        ("redis://u:p@host#", "redis://u:***@host#"),
        ("redis://u:p@host/0?", "redis://u:***@host/0?"),
        ("redis://u:p@host/0#", "redis://u:***@host/0#"),
    ):
        out = redact_url(shape)
        assert out == expected, (
            f"AC2: an empty query/fragment delimiter must survive, got {out!r}"
        )
    # PATH CONSISTENCY (closeout NOTE-1): an "@"-bearing username must be
    # preserved the same way on every path — the structured rebuild keeps
    # "to@ken:", and so must the fallback (multi-host) and crossed paths.
    assert redact_url("redis://to@ken:hunter2@host") == (
        "redis://to@ken:***@host"
    ), "AC2: structured path must preserve an @-bearing username"
    assert redact_url("redis+sentinel://to@ken:pw@h1,h2/0") == (
        "redis+sentinel://to@ken:***@h1,h2/0"
    ), "AC2: fallback path must preserve an @-bearing username"
    assert redact_url("redis://to@ken:hunter2?x@host") == (
        "redis://to@ken:***@host"
    ), "AC2: crossed path must preserve an @-bearing username"
