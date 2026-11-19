"""Helpers for redacting secrets from connection strings before logging.

Connection URLs (Redis, databases) can embed credentials as
``scheme://user:password@host``. Logging such a URL verbatim leaks the
password into log aggregators. Use :func:`redact_url` whenever a connection
URL may appear in a log message or exception detail.

Beyond the ``user:password@`` pair, two more credential shapes are masked
(issue #1171): a username-only userinfo (``redis://token@host`` — redis-py
AUTHs with the bare token) and ``password=`` / ``username=`` query parameters
(redis-py decodes and honors both, including percent-encoded key names).
Malformed credential-bearing shapes that cannot be safely re-rendered are
masked fail-closed rather than returned verbatim.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

# Matches a leading credential pair in the userinfo (``user:pass@`` or
# ``:pass@``). Used as a fast path and a fallback when urlsplit cannot parse
# the scheme (e.g. ``redis+sentinel://``). The password class stops at ``/``:
# URLs merely CONTAINING ``@`` later in the path or query
# (``redis://host:6379/0?email=a@b``) are credential-free and must not match.
_CREDENTIALS_RE = re.compile(r"(?<=://)([^/@:]*):[^/@]*@")

# A username-only userinfo (no colon): the bare token is itself the
# credential (redis-py AUTH). Non-empty capture only — ``scheme://@host``
# carries no credential and passes through.
_USERNAME_ONLY_RE = re.compile(r"(?<=://)([^/@:?#]+)@")

# Empty-username password that itself contains an unescaped ``/``
# (``redis://:Ab3/xYz9+Q@redis:6379/0``): urlsplit truncates the netloc at
# the slash (hostname None), so the structured rebuild never fires and the
# pair regex above cannot cross the slash. The colon must sit IMMEDIATELY
# after ``://`` (empty username) — a host like ``host:6379/...`` does not
# match, and ``?``/``#`` bound the password so a query ``@`` cannot.
_EMPTY_USERINFO_SLASH_PW_RE = re.compile(r"(?<=://):[^@?#]*@")

# Query-string credentials: redis-py parses the query with parse_qs (keys are
# percent-DECODED before matching), so classification decodes the raw key
# name while the redaction itself is a raw-span value replacement that
# preserves key spelling, unrelated parameters, order, and duplicates.
_CREDENTIAL_QUERY_KEYS = frozenset({"password", "username"})
_QUERY_SPAN_RE = re.compile(r"([^=&#]+)=([^&#]*)")

# A netloc of exactly ``:<token>`` with no ``@``: when the token is not all
# digits it is credential-shaped (``redis://:sup3rs3cretpw`` makes redis-py
# try to cast it as a port); a digits-only token is a port and is preserved.
_LEADING_COLON_NETLOC_RE = re.compile(r"(?<=://):([^/?#]+)")


def _redact_query_credentials(url: str) -> str:
    """Redact ``password=``/``username=`` values in the QUERY string only.

    Raw-span: only the matched values are replaced; key spelling (including
    percent-encoding), unrelated parameters, their order, duplicate
    parameters, and any ``#fragment`` are preserved byte-for-byte.
    """
    qpos = url.find("?")
    if qpos < 0:
        return url
    head, query = url[: qpos + 1], url[qpos + 1 :]
    fragment_at = query.find("#")
    if fragment_at >= 0:
        query, fragment = query[:fragment_at], query[fragment_at:]
    else:
        fragment = ""

    def _redact_value(match: re.Match[str]) -> str:
        raw_key = match.group(1)
        decoded_key = unquote(raw_key).strip().lower()
        if decoded_key in _CREDENTIAL_QUERY_KEYS:
            return f"{raw_key}=***"
        return match.group(0)

    return head + _QUERY_SPAN_RE.sub(_redact_value, query) + fragment


def redact_url(url: str) -> str:
    """Return a connection URL with any embedded credential masked.

    Handles ``redis://``, ``rediss://``, ``redis+sentinel://`` (including
    multi-host sentinel/cluster forms), ``mongodb://``, and similar
    ``scheme://[user[:password]@]host`` forms. Also masks a username-only
    userinfo (``scheme://token@host``) and ``password=``/``username=`` query
    parameters (both are live credentials for redis-py), and fails closed on
    malformed credential-bearing netlocs (``scheme://:token`` with a
    non-numeric token) instead of echoing them verbatim. Schemes without
    credentials (e.g. ``memory://``, ``redis://localhost:6379/0``) are
    returned unchanged.

    Never raises on string input: any parse failure (e.g. a multi-host URL whose
    ``host:port,host:port`` netloc makes ``.port`` raise ``ValueError``) falls
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
        >>> redact_url("redis://TOKENuser@redis:6379/0")
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

    url = _redact_query_credentials(url)

    parsed = urlsplit(url)
    # ``.port`` is a property that raises ValueError on multi-host netlocs
    # (e.g. ``h1:26379,h2:26379``). Only use the structured rebuild path when
    # the netloc is a clean single host (port parses cleanly AND there is no
    # comma, which would indicate a cluster/sentinel host list). On any parse
    # ambiguity, fall through to the regex so the helper never raises and never
    # drops host list entries.
    netloc = parsed.netloc if parsed.scheme else ""
    if (
        parsed.scheme
        and parsed.hostname is not None
        and "," not in netloc  # single host only (no cluster/sentinel list)
        and parsed.password is not None  # only rebuild when there's a secret to mask
    ):
        try:
            port = parsed.port
        except ValueError:
            port = None
        userinfo = parsed.username or ""
        rebuilt = f"{userinfo}:***@{parsed.hostname}"
        if port is not None:
            rebuilt += f":{port}"
        return urlunsplit(parsed._replace(netloc=rebuilt))

    # Fallback: multi-host/cluster netlocs, schemes urlsplit doesn't fully
    # parse, or strings without a netloc. Regex-mask any ``user:pass@`` pair
    # (password class bounded at ``/`` so a later path/query ``@`` cannot
    # match), then the empty-username unescaped-slash password arm, then a
    # colon-less username-only userinfo, then fail closed on a
    # credential-shaped ``:<token>`` netloc; otherwise return as-is.
    masked = _CREDENTIALS_RE.sub(r"\1:***@", url, count=1)
    masked = _EMPTY_USERINFO_SLASH_PW_RE.sub(":***@", masked, count=1)
    masked = _USERNAME_ONLY_RE.sub("***@", masked, count=1)
    if masked == url:
        # No userinfo matched at all. A netloc of exactly ``:<token>`` with a
        # non-numeric token is credential-shaped (redis-py casts it as a port
        # and fails) — mask the token rather than echo it.
        m = _LEADING_COLON_NETLOC_RE.search(url)
        if m is not None and not m.group(1).isdigit():
            masked = f"{url[: m.start(1)]}***"
    return masked
