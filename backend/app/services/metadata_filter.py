"""Typed user-facing metadata filtering for chat retrieval (issue #510 AC-16).

Resolves a ``MetadataFilter`` (date/tag/author subset accepted by the chat API)
into a LanceDB ``filter_expr`` over chunk ``file_id`` columns:

- ``date_from`` / ``date_to`` — ``files.document_date`` (ISO date text range).
  Files with no recorded document date are EXCLUDED while a date filter is
  active (documented contract: an unknown date cannot satisfy a range).
- ``tags`` — the vault-scoped ``document_tags`` JOIN ``tags`` tables.
- ``author`` — ``files.email_sender`` (the only persisted author-like field;
  recorded only for email-sourced documents, which is documented rather than
  silently ignored).

Vault scoping: when ``vault_id`` is provided, every resolution query is
constrained to that vault, so the resolved file-id set can never cross vault
boundaries. When ``vault_id`` is None (the admin "All Vaults" surface), the
resolution is intentionally unscoped — admin callers already hold cross-vault
read authority, and the filter narrows within the full corpus.

Zero-match semantics: when no file satisfies the filter — including when an
explicitly supplied field has an empty value (an empty author or an empty
tag name is a filter that matches nothing, never a filter that matches
everything) — or the metadata database is unavailable, the resolver returns
the zero-match sentinel ``file_id IN ('')`` which matches no chunk. A
user-supplied filter is NEVER silently dropped: "no documents match" is a
real, visible result. The
unavailability fallback is typed to the three classes the checkout/query
surface can actually raise for environmental causes — ``OSError``
(filesystem/parent-directory failure in pool connection creation),
``RuntimeError`` (pool closed or checkout wait exhausted), and
``sqlite3.Error`` (schema or database-file problems). Everything else —
notably ``TypeError`` from signature/arity drift — is a programming error
and propagates to the caller instead of masquerading as "no matches"
(issue #707).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import Any, Optional, Set

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Sentinel expression: truthy for the caller, matches no chunk (file ids are
# non-empty strings).
ZERO_MATCH_FILTER_EXPR = "file_id IN ('')"


class MetadataFilter(BaseModel):
    """Typed, documented metadata-filter subset (unknown fields rejected)."""

    model_config = ConfigDict(extra="forbid")

    date_from: Optional[date] = None
    date_to: Optional[date] = None
    tags: Optional[list[str]] = Field(default=None)
    author: Optional[str] = None


def _escape_sql_string(value: str) -> str:
    return value.replace("'", "''")


def _file_id_in_expr(file_ids: Set[str]) -> str:
    if not file_ids:
        return ZERO_MATCH_FILTER_EXPR
    quoted = ", ".join(
        f"'{_escape_sql_string(str(fid))}'" for fid in sorted(file_ids)
    )
    return f"file_id IN ({quoted})"


def resolve_metadata_filter(
    metadata_filter: Any,
    vault_id: Optional[int],
) -> Optional[str]:
    """Resolve a filter payload into a LanceDB filter expression.

    Args:
        metadata_filter: a ``MetadataFilter`` model, a plain dict with the
            same fields (validation errors propagate to the caller — unknown
            fields must never be silently ignored), or ``None``.
        vault_id: vault scope for every resolution query.

    Returns:
        ``None`` when no filter was supplied (caller must NOT filter);
        otherwise a non-empty ``filter_expr`` string — either the resolved
        ``file_id IN (...)`` set or the zero-match sentinel.
    """
    if metadata_filter is None:
        return None
    if isinstance(metadata_filter, dict):
        parsed = MetadataFilter(**metadata_filter)
    elif isinstance(metadata_filter, MetadataFilter):
        parsed = metadata_filter
    else:
        parsed = MetadataFilter.model_validate(metadata_filter)

    if (
        parsed.date_from is None
        and parsed.date_to is None
        and not parsed.tags
        and parsed.author is None
    ):
        # All-None payload is equivalent to no filter (the typed model
        # guarantees no unknown fields reached this point).
        return None

    file_ids = _resolve_file_ids(parsed, vault_id)
    return _file_id_in_expr(file_ids)


def _resolve_file_ids(
    parsed: MetadataFilter, vault_id: Optional[int]
) -> Set[str]:
    """Vault-scoped file-id resolution across the supported filter fields."""
    # Import here (not from rag_engine) so engine-level pool patching cannot
    # break resolution, and to avoid an import cycle at module load.
    # settings is the same configured source every other get_pool caller
    # uses (rag_engine._get_pool idiom, issue #707).
    from app.config import settings
    from app.models.database import get_pool

    conditions: list[str] = []
    params: list[Any] = []
    if vault_id is not None:
        conditions.append("f.vault_id = ?")
        params.append(int(vault_id))
    if parsed.date_from is not None:
        conditions.append("f.document_date IS NOT NULL AND f.document_date >= ?")
        params.append(parsed.date_from.isoformat())
    if parsed.date_to is not None:
        conditions.append("f.document_date IS NOT NULL AND f.document_date <= ?")
        params.append(parsed.date_to.isoformat())
    # `is not None`, not truthiness: an empty-string author is a
    # USER-SUPPLIED filter, and a truthiness test silently dropped the
    # predicate entirely, making ``{"author": ""}`` resolve to EVERY file in
    # scope (issue #707 review F-02). SQL ``= ''`` does not match NULL, so an
    # empty author now yields the zero-match sentinel — the same contract the
    # empty-string-tag case already has.
    if parsed.author is not None:
        conditions.append("f.email_sender = ?")
        params.append(parsed.author)

    join_clause = ""
    if parsed.tags:
        # Vault-scoped join through the tag assignment tables. The redundant
        # t.vault_id = f.vault_id check is defense-in-depth per repo
        # convention (guard the join, not just the outer query).
        placeholders = ", ".join("?" for _ in parsed.tags)
        join_clause = (
            " JOIN document_tags dt ON dt.file_id = f.id"
            " JOIN tags t ON t.id = dt.tag_id AND t.vault_id = f.vault_id"
        )
        conditions.append(f"t.name IN ({placeholders})")
        params.extend(parsed.tags)

    # All interpolated fragments are fixed literal SQL (column/table names,
    # join clauses, fixed condition templates); every value is bound via "?"
    # parameters. Bandit cannot see that, hence the targeted suppression.
    where = " AND ".join(conditions) if conditions else "1=1"
    query = (
        "SELECT DISTINCT f.id FROM files f"
        f"{join_clause} WHERE {where}"  # nosec B608 — literals only; values parameterized
    )

    try:
        pool = get_pool(str(settings.sqlite_path))
        with pool.connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return {str(row["id"]) for row in rows}
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        # Typed unavailability fallback (pool closed / checkout exhausted,
        # filesystem failure, schema or DB-file problems): the documented
        # zero-match sentinel applies. Programming errors (e.g. TypeError
        # from signature drift) deliberately do NOT match this clause and
        # propagate (issue #707).
        logger.warning(
            "Metadata filter resolution failed (zero-match sentinel applied, "
            "filter NOT ignored): %s",
            exc,
        )
        return set()
