"""Helpers for redacting secrets from connection strings before logging.

Connection URLs (Redis, databases) can embed credentials as
``scheme://user:password@host``. Logging such a URL verbatim leaks the
password into log aggregators. Use :func:`redact_url` whenever a connection
URL may appear in a log message or exception detail.

Beyond the ``user:password@`` pair, three more credential shapes are masked
(issue #1171): a username-only userinfo (``redis://token@host`` — redis-py
AUTHs with the bare token, including tokens that themselves contain ``@``),
``password=`` / ``username=`` query parameters (redis-py decodes and honors
both, including percent-encoded key names), and any other
``...password``-suffixed query key (``ssl_password`` is the TLS private-key
passphrase redis-py 8.x routes to SSLConnection). Malformed credential-bearing
shapes that cannot be safely re-rendered are masked fail-closed rather than
returned verbatim.

Malformed shapes where a password span crosses ``?``/``#``
(``redis://user:hunter2?x@host``) are masked fail-closed by the
qmark/hash-pair arm: the pre-delimiter colon token is disambiguated from a
``host:port`` prefix by the digits rule (an all-digits pre-delimiter token is
a port — ``redis://host:6379?email=a@b`` passes through untouched), so the
mask may cross the delimiter and still never reach a credential-free URL.
An EMPTY single-``@`` username-only userinfo (``redis://@host``) is a
degenerate shape carrying no credential bytes and passes through; any userinfo
with at least one non-empty ``@``-terminated token (``redis://to@@ken@host`` —
urlsplit's rpartition makes ``to@@ken`` the username) or a double ``@``
(``redis://@@host`` — rpartition makes ``@`` a non-empty username) is masked
whole.

SCOPE: the classifier masks credentials of the URL it is GIVEN — its own
authority (userinfo) and its own query (credential-keyed parameters). Every
userinfo arm is bounded to the OUTER authority (the region after the scheme's
``://`` and before its first ``/``, ``?`` or ``#``), so a URL EMBEDDED in a
query value or fragment (``redis://host/0?next=http://user:pass@h``) is
opaque text and stays byte-for-byte — it is a different URL, not this one's
userinfo, and rewriting it would corrupt the outer URL's structure. The
outer URL's own credentials are still masked in that shape
(``redis://host/0?password=OUTER&next=...`` masks ``password=OUTER``).
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

# Matches a leading credential pair in the userinfo (``user:pass@`` or
# ``:pass@``). Used as a fast path and a fallback when urlsplit cannot parse
# the scheme (e.g. ``redis+sentinel://``). The username class stops at ``/``,
# ``?``, ``#``, ``@`` and ``:``; the password class stops at ``/``, ``?`` and
# ``#`` but MAY contain ``@`` — urlsplit assigns userinfo by the LAST ``@``
# of the netloc (rpartition), so ``svc:p@ss@h1,...`` carries password ``p@ss``
# and the whole of it is masked. Because the classes stop at ``/``, ``?`` and
# ``#``, URLs merely CONTAINING ``@`` later in the path or query
# (``redis://host:6379/0?email=a@b``, ``redis://host:6379?email=a@b``) are
# credential-free and cannot match.
_CREDENTIALS_RE = re.compile(r"(?<=://)([^/@:?#]*):[^/?#]*@")

# A username-only userinfo (no colon before the separator ``@``): the bare
# token is itself the credential (redis-py AUTH), and a token may itself
# contain ``@`` (``redis://to@ken@host`` — urlsplit's rpartition makes
# ``to@ken`` the username), including empty tokens between consecutive ``@``s
# (``redis://to@@ken@host`` makes ``to@@ken`` the username). One or more
# ``@``-terminated tokens (possibly empty) are consumed so the WHOLE userinfo
# is masked; the sub-replacement passes through a SINGLE bare ``@`` match
# (``redis://@host`` — an EMPTY username, no credential bytes) and masks any
# longer match (``redis://@@host`` — rpartition makes ``@`` a non-empty
# username). Tokens cannot cross ``/``, ``?``, ``#`` or `:`, so path/query
# ``@`` characters and ``user:pass@`` pairs (already masked by the pair arm
# above) cannot feed this arm.
_USERNAME_ONLY_RE = re.compile(r"(?<=://)(?:[^/:?#@]*@)+(?![^/?#]*@)")

# QMARK/HASH-PAIR SHAPES: a password span that crosses an unescaped ``?`` or
# ``#`` (``redis://user:hunter2?x@host``) cannot be matched by the pair arm
# above (its password class stops at ``?``/``#``) and urlsplit terminates the
# netloc there — but the pre-delimiter ``user:token`` shape is
# disambiguated from a ``host:port`` prefix by the DIGITS RULE: an
# all-digits token is a port (``redis://host:6379?email=a@b`` passes through
# untouched, byte-for-byte), anything else is credential-shaped and the mask
# crosses the delimiter through the ``@`` (``redis://user:***@host``). A
# ``/`` anywhere before the ``@`` terminates this arm (a path segment cannot
# be a password); the ambiguous-slash arm below owns those shapes.
_QMARK_HASH_PAIR_RE = re.compile(
    r"(?<=://)([^/?#:]*):([^/?#@]*)(?:[?#](?:[^@#:]|:(?!//))*)?@"
)

# CROSSED-CREDENTIAL ARM (Stage B NEW-4/5/6): urlsplit terminates the netloc
# at the FIRST ``/``, ``?`` or ``#``, so for a password span that crosses any
# of them (``redis://user:secret?part@TAILSECRET@host``,
# ``redis://user:secret?part#TAILSECRET@host``,
# ``redis://user:secret/x@TAILSECRET@host``) the structured rebuild masks
# only ``user:secret`` and renders the remaining tail verbatim. This arm
# runs BEFORE the structured rebuild: a ``user:token`` prefix (token
# non-digits) followed IMMEDIATELY by a netloc-terminating delimiter is
# treated as ONE crossed credential span and masked through the LAST ``@``
# of the URL (rpartition semantics like urlsplit's own userinfo rule). The
# digits rule protects port shapes (``redis://host:6379?email=a@b``,
# ``redis://:9000/path@host``, ``redis://user:123/secret@host``), and a
# prefix not immediately followed by a delimiter
# (``user:pw@h1,h2?email=a@b``) cannot match because the token class
# excludes ``@``. Implemented as prefix-regex + last-``@`` search rather
# than one regex because the tail must span further ``?``/``#``/``/``
# delimiters, which no single bounded class admits.
_CROSS_PREFIX_RE = re.compile(r"(?<=://)([^/@?#:]*):([^/?#@]+)[?#/]")


def _is_outer_authority_start(url: str, pos: int) -> bool:
    """True when ``pos`` lies within the OUTER authority — the region after
    the scheme's first "://" and before its first "/", "?" or "#" (closeout
    NEW-13/NEW-14: a query value or fragment may embed a whole URL whose
    "://" would otherwise satisfy the lookbehind; a match starting after
    the outer authority's first delimiter is nested text, not userinfo)."""
    si = url.find("://")
    if si < 0:
        return False
    authority_start = si + 3
    if pos < authority_start:
        return False
    term = len(url)
    for ch in "/?#":
        idx = url.find(ch, authority_start)
        if 0 <= idx < term:
            term = idx
    return pos <= term


def _crossed_span_end(rest: str) -> int | None:
    """One-past the crossed credential's final ``@`` within ``rest`` (the
    text after the first netloc-terminating delimiter), or ``None``.

    The separator ``@`` walk: scanning left to right, a gap between two
    consecutive ``@``s that contains ``/``, ``?`` or ``#`` means the right
    ``@`` sits in host/path/query text — the LEFT ``@`` is the userinfo
    separator (``redis://user:secret/x@host/0?email=a@b`` must keep
    ``host/0?email=a@b``). When every inter-``@`` gap is delimiter-free the
    whole span is one multi-``@`` credential and the LAST ``@`` wins
    (``...?part@TAILSECRET@host``, ``.../a/b@TAILSECRET@host``).

    An EMBEDDED url inside the region owns every ``@`` after its own
    ``://`` — those belong to that url, not to this credential (closeout
    NEW-16), so the walk stops at the first ``://``; ``None`` then means
    "this region is opaque embedded text" and the caller masks only the
    credential PREFIX.
    """
    limit = rest.find("://")
    if limit < 0:
        limit = len(rest)
    positions = [i for i, ch in enumerate(rest[:limit]) if ch == "@"]
    if not positions:
        return None
    end = positions[-1] + 1
    for prev, nxt in zip(positions, positions[1:]):
        gap = rest[prev + 1 : nxt]
        if "/" in gap or "?" in gap or "#" in gap:
            return prev + 1
    return end

# AMBIGUOUS SLASH SHAPES: an unescaped ``/`` inside the password truncates
# urlsplit's netloc (hostname None), so the structured rebuild never fires
# and the pair regex above cannot cross the slash. ``user:pass/x...@`` and
# ``:pass/x...@`` (empty username) are credential-bearing; ``host:6379/x...@``
# is a host:PORT plus a path whose segment merely contains ``@``. The
# disambiguator is the token between ``:`` and the first ``/``: all digits
# means port (leave unchanged — ``redis://:9000/path@host``), anything else
# is credential-shaped and masked fail-closed (``redis://user:Ab3/xYz9+Q@h``,
# ``redis://:Ab3/xYz9+Q@h``). ``?``/``#`` bound the password span so a
# query/fragment ``@`` cannot feed this arm.
_AMBIGUOUS_SLASH_CREDENTIAL_RE = re.compile(
    r"(?<=://)([^/@:?#]*):([^/@?#]*)/[^/@?#]*@"
)

# Query-string credentials: redis-py parses the query with parse_qs (keys are
# percent-DECODED before matching), so classification decodes the raw key
# name while the redaction itself is a raw-span value replacement that
# preserves key spelling, unrelated parameters, order, and duplicates.
# ``username`` is the AUTH user; any ``...password`` key is masked fail-closed
# by the suffix rule (``password`` itself and ``ssl_password`` — the TLS
# private-key passphrase redis-py 8.x routes to SSLConnection — are the
# documented members).
_CREDENTIAL_QUERY_KEYS = frozenset({"username"})


def _is_credential_query_key(decoded_key: str) -> bool:
    return decoded_key in _CREDENTIAL_QUERY_KEYS or decoded_key.endswith(
        "password"
    )

# A netloc of exactly ``:<token>`` with no ``@``: when the token is not all
# digits it is credential-shaped (``redis://:sup3rs3cretpw`` makes redis-py
# try to cast it as a port); a digits-only token is a port and is preserved.
_LEADING_COLON_NETLOC_RE = re.compile(r"(?<=://):([^/?#]+)")


def _mask_credential_segments(text: str) -> str:
    """Apply the per-segment credential-key redaction to ``&``-separated
    ``key=value`` text (used for both a real query and the orphaned-query
    sweep). Only credential-keyed values are replaced; key spelling,
    unrelated parameters, their order, and duplicates are preserved.
    """
    masked_params = []
    for segment in text.split("&"):
        eq = segment.find("=")
        if eq < 0:
            masked_params.append(segment)
            continue
        raw_key = segment[:eq]
        decoded_key = unquote(raw_key).strip().lower()
        if _is_credential_query_key(decoded_key):
            masked_params.append(f"{raw_key}=***")
        else:
            masked_params.append(segment)
    return "&".join(masked_params)


def _redact_query_credentials(url: str, orphan_sweep: bool = False) -> str:
    """Redact ``password=``/``username=`` values in the QUERY string only.

    Raw-span: only the matched values are replaced; key spelling (including
    percent-encoding), unrelated parameters, their order, duplicate
    parameters, and any ``#fragment`` are preserved byte-for-byte. Runs
    AFTER the userinfo arms (``_mask_userinfo``): a query value that
    contains ``@`` (``redis://user:hunter2?password=x@host``) must not
    consume the ``@`` the userinfo arms anchor on, or the userinfo password
    would survive (Stage B NEW-2).

    ``orphan_sweep`` is True only when the ORIGINAL url carried a real query
    ("?" before any "#") and the userinfo mask consumed it — see the sweep
    branch below.
    """
    # The query starts at the first "?" ONLY IF it precedes the fragment:
    # a "?" after the "#" is fragment text (urlparse gives .query == "" for
    # ``scheme://host/#section?password=x``), and redis-py honors only the
    # real query — fragment bytes are preserved untouched.
    fpos = url.find("#")
    scope = url if fpos < 0 else url[:fpos]
    if orphan_sweep:
        # ORPHANED-QUERY SWEEP (closeout review): the userinfo mask consumed
        # the ORIGINAL query delimiter (proven by the "?" count dropping —
        # the arms only delete bytes), so ANY surviving "?" is a value
        # character of the orphaned query, not a new delimiter. Replace the
        # normal scan entirely: apply the credential-key segment mask to the
        # scope after the mask's separator "@" (the FIRST "@" — the same
        # boundary the gap walk uses, never a later path "@"), fragment
        # split off so fragment bytes stay untouched.
        at = url.find("@")
        if at < 0:
            return url
        tail = url[at + 1 :]
        tail_fpos = tail.find("#")
        if tail_fpos >= 0:
            tail, fragment = tail[:tail_fpos], tail[tail_fpos:]
        else:
            fragment = ""
        masked_tail = _mask_credential_segments(tail)
        if masked_tail == tail:
            return url
        return url[: at + 1] + masked_tail + fragment
    qpos = scope.find("?")
    if qpos < 0:
        # No query here and the original had none either: every byte is
        # credential-free per the classifier — return untouched (the
        # fragment of a no-query URL is never rewritten).
        return url
    rest = url[qpos + 1 :]
    # Fragment bytes are split off BEFORE the '='-absence guard, so a long
    # '='-less query followed by a fragment that merely CONTAINS '=' (a
    # fragment value like ``#x=y``) cannot defeat the guard and force a
    # scan (feedback round).
    fragment_at = rest.find("#")
    if fragment_at >= 0:
        query, fragment = rest[:fragment_at], rest[fragment_at:]
    else:
        query, fragment = rest, ""
    if "=" not in query:
        # No key=value span can exist in a '='-less query; returning here
        # keeps long '='-less inputs off the per-segment scan entirely.
        return url
    # Segment scan (not one regex over the whole query): a key class that
    # may span 100KB of '='-less characters backtracks quadratically when a
    # LATER parameter carries '='; per-segment str.find is linear in the
    # query length (Stage B NEW-2 review). Semantics match the former
    # ``([^=&#]+)=([^&#]*)`` sub: key = segment text up to its FIRST '=',
    # value = the rest of that segment, only credential-keyed values are
    # replaced, segments without '=' pass through.
    head = url[: qpos + 1]
    return head + _mask_credential_segments(query) + fragment


def redact_url(url: str) -> str:
    """Return a connection URL with any embedded credential masked.

    Handles ``redis://``, ``rediss://``, ``redis+sentinel://`` (including
    multi-host sentinel/cluster forms), ``mongodb://``, and similar
    ``scheme://[user[:password]@]host`` forms. Also masks a username-only
    userinfo (``scheme://token@host``, including tokens that contain ``@``,
    even across empty tokens between consecutive ``@``s —
    ``scheme://to@@ken@host``), ``password=``/``username=`` query parameters,
    and any other ``...password``-suffixed query key (live credentials for
    redis-py — ``ssl_password`` is the TLS private-key passphrase redis-py
    8.x routes to SSLConnection), and fails closed on malformed
    credential-bearing netlocs (``scheme://:token`` with a non-numeric token)
    and on password spans that cross ``?``/``#``
    (``redis://user:hunter2?x@host`` -> ``redis://user:***@host``) instead of
    echoing them verbatim. Schemes without credentials (e.g. ``memory://``,
    ``redis://localhost:6379/0``) are returned unchanged.

    Degenerate shapes carry no credential bytes and pass through: an empty
    single-``@`` userinfo (``redis://@host``) and an all-digits token
    before ``?``/``#`` or before ``/`` (``redis://host:6379?email=a@b`` —
    a port plus a query, urlsplit agrees the URL carries no credential).
    ``redis://@@host`` masks: urlsplit's rpartition makes ``@`` a non-empty
    username.

    Scope: the credentials masked are the ones of THIS url — its authority
    userinfo and its own query parameters. Text that merely embeds another
    url (a query value or fragment holding ``http://user:pass@h``) is
    opaque and byte-preserved; see the module docstring.


    Never raises on string input: any parse failure (a multi-host netloc
    whose ``.port`` raises, a bracket-malformed IPv6 netloc, ...) falls
    through to a regex that masks a ``user:password@`` pair, or returns the
    string as-is. Safe to call inside a logging path.

    Examples:
        >>> redact_url("redis://localhost:6379/0")
        'redis://localhost:6379/0'
        >>> redact_url("redis://:s3cret@redis:6379/2")
        'redis://:***@redis:6379/2'
        >>> redact_url("redis://app:p%40ss@redis:6379/0")
        'redis://app:***@redis:6379/0'
        >>> redact_url("redis+sentinel://:pw@h1:26379,h2:26379/mymaster/0")
        'redis+sentinel://:***@h1:26379,h2:26379/mymaster/0'
        >>> redact_url("redis://svc:p@ss@h1:26379,h2:26379/mymaster/0")
        'redis://svc:***@h1:26379,h2:26379/mymaster/0'
        >>> redact_url("redis://to@ken@redis:6379/0")
        'redis://***@redis:6379/0'
        >>> redact_url("redis://redis:6379/0?password=QUERYsecret")
        'redis://redis:6379/0?password=***'
        >>> redact_url("redis://:6379")
        'redis://:6379'
        >>> redact_url("memory://")
        'memory://'
    """
    if not url:
        return url

    # USERINFO ARMS FIRST (Stage B NEW-2): a query value that contains "@"
    # (``redis://user:hunter2?password=x@host``) must not consume the "@"
    # the userinfo arms anchor on — running the query scan first would
    # replace ``x@host`` with ``x=***``-style bytes and leave the userinfo
    # password unmasked. Query redaction then runs on the masked text; the
    # orphaned-query sweep is armed ONLY when the original carried a real
    # query and the mask consumed it (closeout review: a no-query URL's
    # fragment/path bytes are never rewritten).
    fpos = url.find("#")
    original_scope = url if fpos < 0 else url[:fpos]
    had_query = "?" in original_scope
    masked = _mask_userinfo(url)
    masked_scope = masked.split("#", 1)[0]
    # Delimiter identity by COUNT, not presence (closeout NEW-11): a "?"
    # inside a query VALUE (``...&next=?foo``) survives masking, so "? in
    # masked" cannot decide whether the ORIGINAL delimiter was consumed.
    # The masks only ever delete bytes (they insert "***@"), so a reduced
    # "?" count proves the original delimiter was consumed.
    query_consumed = (
        had_query and masked_scope.count("?") < original_scope.count("?")
    )
    return _redact_query_credentials(masked, orphan_sweep=query_consumed)


def _mask_userinfo(url: str) -> str:
    """Mask credential-shaped userinfo spans (qmark-hash-cross / pair /
    qmark-hash / slash / username-only / leading-colon arms). Query strings
    are NOT touched here.
    """
    # CROSSED-CREDENTIAL first (Stage B NEW-4/5/6): the structured rebuild
    # below trusts urlsplit's netloc, which terminates at the first ``/``,
    # ``?`` or ``#`` — for a crossed password span that would leave the tail
    # bytes verbatim in the rendered URL. When the crossed shape holds
    # (non-digits token immediately before the delimiter), mask the WHOLE
    # span through the URL's last ``@`` up front; the remaining arms then see
    # already-masked text and stay idempotent.
    crossed = _CROSS_PREFIX_RE.search(url)
    if (
        crossed is not None
        and not crossed.group(2).isdigit()
        # OUTER-AUTHORITY GUARD (closeout NEW-12/13/14): a query value or
        # fragment may embed a whole URL whose "://" satisfies the
        # lookbehind; a crossed credential prefix always begins within the
        # OUTER authority (right after the scheme's "://", before the first
        # "/", "?" or "#").
        and _is_outer_authority_start(url, crossed.start(1))
    ):
        rest = url[crossed.end() :]
        end = _crossed_span_end(rest)
        if end is not None:
            cut = crossed.end() + end
            url = (
                url[: crossed.start(1)]
                + f"{crossed.group(1)}:***@"
                + url[cut:]
            )
        # end is None -> the region carries no crossed separator of its own
        # (the "@"s belong to an EMBEDDED url, closeout NEW-16): this is a
        # host:port prefix, not a crossed credential, so it is left alone
        # and the embedded url's bytes stay byte-for-byte.
    # urlsplit raises ValueError on bracket-malformed IPv6 netlocs before any
    # parse result exists; fall through to the non-raising regex fallback so
    # the never-raises contract holds (the fallback masks the
    # credential-shaped spans of those inputs fail-closed).
    try:
        parsed = urlsplit(url)
    except ValueError:
        parsed = None
    netloc = parsed.netloc if parsed is not None and parsed.scheme else ""
    if (
        parsed is not None
        and parsed.scheme
        and parsed.hostname is not None
        and "," not in netloc  # single host only (no cluster/sentinel list)
        and parsed.password is not None  # only rebuild when there's a secret to mask
    ):
        # Rebuild from the RAW netloc tail (rpartition on '@'): parsed.hostname
        # strips IPv6 brackets and a malformed port is dropped by the .port
        # property, so the raw host:port text is what keeps the rendered URL
        # faithful (``user:***@[::1]:6379``, ``:***@host:notaport``).
        #
        # The netloc is SPLICED, not re-assembled: urlunsplit drops an empty
        # "?"/"#" delimiter (``redis://u:p@host?`` would lose its "?" —
        # closeout NEW-17), so only the netloc span is replaced and every
        # other byte of the original url is kept verbatim.
        _, _, hostpart = netloc.rpartition("@")
        rebuilt = f"{parsed.username or ''}:***@{hostpart}"
        # The netloc starts right after the scheme's "://" (a text search
        # could hit an earlier coincidence of the same bytes).
        scheme_end = url.find("://")
        netloc_start = scheme_end + 3 if scheme_end >= 0 else -1
        if netloc_start < 0 or url[netloc_start : netloc_start + len(netloc)] != netloc:
            netloc_start = url.find(netloc)
        if netloc_start < 0:
            return urlunsplit(parsed._replace(netloc=rebuilt))
        return url[:netloc_start] + rebuilt + url[netloc_start + len(netloc) :]

    # Fallback: multi-host/cluster netlocs, schemes urlsplit doesn't fully
    # parse, bracket-malformed netlocs, or strings without a netloc.
    # Regex-mask any ``user:pass@`` pair (the password may contain ``@`` —
    # urlsplit's rpartition semantics — but cannot cross ``/``, ``?``, ``#``
    # so a later path/query ``@`` cannot match), then the ambiguous-slash arm
    # (unescaped ``/`` inside the password, disambiguated from host:port/path
    # by the digits-only test), then a colon-less username-only userinfo
    # (which may itself contain ``@``). All four arms REQUIRE an ``@``, so an
    # ``@``-free string (e.g. a large dense-``://`` config value) skips every
    # scan — keeping the classifier linear on ``@``-free inputs (feedback
    # F-006 dense-:// residual). The leading-colon arm does NOT require an
    # ``@`` and stays OUTSIDE this gate.
    def _top_level_sub(pattern: re.Pattern, repl) -> str:
        """Apply ``pattern.sub`` at the FIRST match that starts within the
        OUTER authority (closeout NEW-13/NEW-14): a query value or fragment
        embedding a URL (``...next=http://user:pass?part@h`` or
        ``...#next=http://user:pass/x@h``) must not feed the userinfo arms,
        or the nested match would rewrite bytes the classifier promises to
        preserve and skew the delimiter count."""
        m = pattern.search(masked)
        while m is not None:
            if _is_outer_authority_start(masked, m.start()):
                return pattern.sub(repl, masked, count=1)
            m = pattern.search(masked, m.end())
        return masked

    masked = url
    if "@" in url:
        masked = _top_level_sub(_CREDENTIALS_RE, r"\1:***@")

        def _qmark_hash_replacement(match: re.Match[str]) -> str:
            username, token = match.group(1), match.group(2)
            if token.isdigit():
                # host:6379?query@x — port plus query, not a credential
                # (same digits disambiguator as the slash arm below).
                return match.group(0)
            return f"{username}:***@"

        masked = _top_level_sub(_QMARK_HASH_PAIR_RE, _qmark_hash_replacement)

        def _ambiguous_replacement(match: re.Match[str]) -> str:
            username, token = match.group(1), match.group(2)
            if token.isdigit():
                return match.group(0)  # host:6379/path@x — port plus path
            return f"{username}:***@"

        masked = _top_level_sub(
            _AMBIGUOUS_SLASH_CREDENTIAL_RE, _ambiguous_replacement
        )

        def _username_only_replacement(match: re.Match[str]) -> str:
            # A SINGLE bare ``@`` (``redis://@host``) is an EMPTY username —
            # no credential bytes, pass through. Two or more ``@``s
            # (``redis://@@host``) make urlsplit's rpartition yield the
            # non-empty username ``@`` — a (degenerate) credential, masked.
            if len(match.group(0)) == 1:
                return match.group(0)
            return "***@"

        masked = _top_level_sub(_USERNAME_ONLY_RE, _username_only_replacement)
    if masked == url:
        # No userinfo matched at all. A netloc of exactly ``:<token>`` with a
        # non-numeric token is credential-shaped (redis-py casts it as a port
        # and fails) — mask the token rather than echo it, preserving
        # whatever path/query follows the netloc. The match must be in the
        # OUTER authority (closeout NEW-15): a query value or fragment may
        # embed a whole URL (``...#next=redis://:notaport``) whose nested
        # "://:token" is not this URL's userinfo and must stay verbatim.
        m = _LEADING_COLON_NETLOC_RE.search(url)
        while m is not None:
            if _is_outer_authority_start(url, m.start(1)):
                if not m.group(1).isdigit():
                    masked = f"{url[: m.start(1)]}***{url[m.end(1) :]}"
                break
            m = _LEADING_COLON_NETLOC_RE.search(url, m.end())
    return masked
