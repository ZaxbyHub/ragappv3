"""Shared predicate for the SQLite constraint class safe to treat as a race.

SQLite reports a violated UNIQUE index as `UNIQUE constraint failed: ...`. A foreign-key
or CHECK failure has a different prefix and must not take the winner-recovery path,
which assumes an existing winning row (issue #1201 AC4).
"""


def is_unique_violation(exc: BaseException) -> bool:
    """True only for a UNIQUE-constraint failure, not any IntegrityError."""
    return str(exc).startswith("UNIQUE constraint failed")
