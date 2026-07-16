"""Helpers for redacting secrets from connection strings before logging.

Connection URLs (Redis, databases) can embed credentials as
``scheme://user:password@host``. Logging such a URL verbatim leaks the
password into log aggregators. Use :func:`redact_url` whenever a connection
URL may appear in a log message or exception detail.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# Matches a leading credential pair in the userinfo (``user:pass@`` or
# ``:pass@``). Used as a fast path and a fallback when urlsplit cannot parse
# the scheme (e.g. ``redis+sentinel://``).
_CREDENTIALS_RE = re.compile(r"(?<=://)([^/@:]*):[^/@]*@")


def redact_url(url: str) -> str:
    """Return a connection URL with any embedded password replaced by ``***``.

    Handles ``redis://``, ``rediss://``, ``redis+sentinel://`` (including
    multi-host sentinel/cluster forms), ``mongodb://``, and similar
    ``scheme://[user[:password]@]host`` forms. Schemes without credentials
    (e.g. ``memory://``, ``redis://localhost:6379/0``) are returned unchanged.

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
        >>> redact_url("memory://")
        'memory://'
    """
    if not url:
        return url

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
    # parse, or strings without a netloc. Regex-mask any ``user:pass@`` pair;
    # otherwise return as-is.
    return _CREDENTIALS_RE.sub(r"\1:***@", url, count=1)
