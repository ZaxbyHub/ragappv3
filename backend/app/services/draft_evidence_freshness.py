"""Evidence freshness and invalidation for Draft Room (SPEC section 12.6).

A compile run snapshots the exact passages it used into ``draft_evidence``.
Those snapshots are immutable history, so nothing in the vault can retroactively
change what a finished revision *said*. What the vault **can** change is whether
that revision is still true, and that is what this module decides.

Three entry points, one rule
----------------------------

1. :func:`check_evidence_freshness` — read-only re-resolution of every evidence
   identity belonging to one compile job, against the draft's *current*
   owner/vault scope. Compares existence, canonical content hash, and
   update/delete metadata.
2. :func:`enforce_evidence_freshness` — the gate. Runs (1) and, if anything
   moved, records the SPEC 12.6 consequences: the current revision becomes
   ``fact_status='invalidated'``, a **non-waivable** ``evidence_changed`` or
   ``source_deleted`` blocker is opened, and a ``ready`` draft drops back to
   ``needs_review``. Callers must run it **before Final Assemble** and **again
   inside the Ready transaction** — the second check is mandatory defense in
   depth even when a mutation hook already ran.
3. :func:`reconcile_ready_evidence` — the bounded, paginated startup pass that
   catches invalidations which happened while this process was down (including
   out-of-band/database-only deletions that no hook could observe).

Plus four best-effort hooks (:func:`on_document_changed`,
:func:`on_wiki_page_changed`, :func:`on_wiki_claim_changed`,
:func:`on_kms_entry_changed`) that the owning services call when a source is
updated or deleted. Every hook is wrapped so it can only ever *log* — it must
never fail, slow, or roll back the host operation it is attached to.

Invariants
----------

* **Evidence rows are never deleted here.** A vanished source stamps
  ``source_deleted_at`` on the snapshot so the passage survives as clearly
  marked history until the whole draft is deleted (SPEC 5.6/12.6). Because
  evidence is keyed by ``job_id`` and every run is a new job, a marked row can
  never be picked up by a later run.
* **Blockers raised here are non-waivable.** SPEC 12.6 requires the text to be
  revised or re-compiled against fresh evidence, not waived.
* **No SQLite connection is held across an ``await``.** The only async function
  here is the reconciler, and it acquires/releases a pooled connection inside
  each :func:`asyncio.to_thread` step.
* **Log IDs and counts only** — never passages, titles, slugs, or paths.

Canonical whole-source hash
---------------------------

SPEC 12.6 defines ``draft_evidence.source_content_sha256`` as the canonical
*whole-source* hash at Research time: the parsed-text hash for a Draft
input/document, the canonical claim content hash for a claim-level Wiki result,
the canonical page content hash for a page-level Wiki result, and the canonical
entry content hash for KMS. :func:`canonical_source_sha256` is the single
definition of that hash on the live row, and re-resolution compares the two.
When the live hash cannot be derived (for example a document row whose
``file_hash`` was never populated) the comparison is skipped rather than
guessed, and existence plus update metadata still apply.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.services.draft_store import (
    EVIDENCE_INVALIDATION_RULE_VERSION,
    DraftEvidenceIdentity,
    DraftStore,
    sha256_text,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EVIDENCE_CHANGED",
    "SOURCE_DELETED",
    "EvidenceIssue",
    "FreshnessResult",
    "ReconcileSummary",
    "canonical_source_sha256",
    "check_evidence_freshness",
    "enforce_evidence_freshness",
    "on_document_changed",
    "on_kms_entry_changed",
    "on_wiki_claim_changed",
    "on_wiki_page_changed",
    "reconcile_ready_evidence",
]

# Reason codes. These are also the ``rule_id`` of the blocker findings, so the
# route layer can map a 409 straight onto the finding a user will see.
EVIDENCE_CHANGED = "evidence_changed"
SOURCE_DELETED = "source_deleted"

# Bounds. Re-resolution walks evidence in pages and stops at a hard ceiling so a
# pathological job can never turn a Ready check (or startup) into an unbounded
# scan. A job that exceeds the ceiling is reported as *not* current — failing
# closed is the only safe direction for an approval gate.
EVIDENCE_PAGE_SIZE = 200
MAX_EVIDENCE_PER_JOB = 5000

# Reconciler bounds (SPEC 12.6: "process in pages").
RECONCILE_PAGE_SIZE = 50
RECONCILE_MAX_DRAFTS = 500


@dataclass(frozen=True)
class EvidenceIssue:
    """One evidence identity that is no longer current.

    ``identity`` is a short ``field=id`` string (for example ``file_id=42``) so
    diagnostics stay to IDs — it never carries source text.
    """

    evidence_id: int
    label: str
    source_kind: str
    reason: str
    identity: str


@dataclass(frozen=True)
class FreshnessResult:
    """Outcome of one re-resolution pass over a job's evidence."""

    job_id: int
    checked: int
    truncated: bool
    issues: tuple[EvidenceIssue, ...]

    @property
    def is_current(self) -> bool:
        """True only when every identity resolved unchanged and nothing was skipped."""
        return not self.issues and not self.truncated

    @property
    def reasons(self) -> tuple[str, ...]:
        """Distinct reason codes, deletion first (it is the stronger statement)."""
        found = {issue.reason for issue in self.issues}
        if self.truncated:
            found.add(EVIDENCE_CHANGED)
        return tuple(r for r in (SOURCE_DELETED, EVIDENCE_CHANGED) if r in found)

    @property
    def evidence_ids(self) -> tuple[int, ...]:
        return tuple(issue.evidence_id for issue in self.issues)


@dataclass(frozen=True)
class ReconcileSummary:
    """What one :func:`reconcile_ready_evidence` pass did. IDs/counts only."""

    drafts_scanned: int
    drafts_invalidated: int
    evidence_checked: int
    truncated: bool


# ---------------------------------------------------------------------------
# Live source resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LiveSource:
    """The current state of one external source, or absence of it."""

    exists: bool
    content_sha256: Optional[str] = None
    updated_at: Optional[str] = None


_ABSENT = _LiveSource(exists=False)


def _one(conn: sqlite3.Connection, sql: str, params: tuple) -> Optional[sqlite3.Row]:
    return conn.execute(sql, params).fetchone()


def _resolve_draft_input(
    conn: sqlite3.Connection, ev: DraftEvidenceIdentity
) -> _LiveSource:
    row = _one(
        conn,
        "SELECT parsed_text_sha256, updated_at FROM draft_inputs "
        "WHERE id = ? AND draft_id = ?",
        (ev.draft_input_id, ev.draft_id),
    )
    if row is None:
        return _ABSENT
    return _LiveSource(True, row[0], row[1])


def _resolve_document(conn: sqlite3.Connection, ev: DraftEvidenceIdentity) -> _LiveSource:
    # Scoped to the draft's vault: a file that moved out of scope is, for this
    # draft, indistinguishable from a deleted one and must be treated as gone.
    row = _one(
        conn,
        "SELECT file_hash, processed_at, modified_at FROM files "
        "WHERE id = ? AND vault_id = ?",
        (ev.file_id, ev.vault_id),
    )
    if row is None:
        return _ABSENT
    return _LiveSource(True, row[0], row[1] or row[2])


def _resolve_wiki(conn: sqlite3.Connection, ev: DraftEvidenceIdentity) -> _LiveSource:
    # SPEC 5.6: "Wiki freshness resolves the page and, when present, the claim."
    # A page-level row (wiki_claim_id IS NULL) is hashed over the page markdown;
    # a claim-level row is hashed over the claim text but still requires its page.
    page = _one(
        conn,
        "SELECT markdown, updated_at FROM wiki_pages WHERE id = ? AND vault_id = ?",
        (ev.wiki_page_id, ev.vault_id),
    )
    if page is None:
        return _ABSENT
    if ev.wiki_claim_id is None:
        return _LiveSource(True, sha256_text(page[0] or ""), page[1])
    claim = _one(
        conn,
        "SELECT claim_text, updated_at, page_id FROM wiki_claims "
        "WHERE id = ? AND vault_id = ?",
        (ev.wiki_claim_id, ev.vault_id),
    )
    if claim is None:
        return _ABSENT
    # A claim re-parented away from the page it was cited under is no longer the
    # same evidence identity.
    if claim[2] is not None and int(claim[2]) != int(ev.wiki_page_id or 0):
        return _ABSENT
    return _LiveSource(True, sha256_text(claim[0] or ""), claim[1])


def _resolve_kms(conn: sqlite3.Connection, ev: DraftEvidenceIdentity) -> _LiveSource:
    row = _one(
        conn,
        "SELECT body, updated_at FROM kms_entries WHERE id = ? AND vault_id = ?",
        (ev.kms_entry_id, ev.vault_id),
    )
    if row is None:
        return _ABSENT
    return _LiveSource(True, sha256_text(row[0] or ""), row[1])


_RESOLVERS: dict[str, Callable[[sqlite3.Connection, DraftEvidenceIdentity], _LiveSource]] = {
    "draft_input": _resolve_draft_input,
    "document": _resolve_document,
    "wiki": _resolve_wiki,
    "kms": _resolve_kms,
}


def canonical_source_sha256(
    conn: sqlite3.Connection, ev: DraftEvidenceIdentity
) -> Optional[str]:
    """Current canonical whole-source hash for ``ev``'s identity, or ``None``.

    ``None`` means either the source no longer resolves in the draft's
    owner/vault scope, or its canonical hash cannot be derived from the live
    row. Callers must distinguish those two cases with
    :func:`check_evidence_freshness` rather than treating ``None`` as "changed".
    """
    resolver = _RESOLVERS.get(ev.source_kind)
    if resolver is None:
        return None
    return resolver(conn, ev).content_sha256


def _identity_label(ev: DraftEvidenceIdentity) -> str:
    """A short ``field=id`` description — IDs only, never content."""
    if ev.source_kind == "draft_input":
        return f"draft_input_id={ev.draft_input_id}"
    if ev.source_kind == "document":
        return f"file_id={ev.file_id}"
    if ev.source_kind == "kms":
        return f"kms_entry_id={ev.kms_entry_id}"
    if ev.wiki_claim_id is not None:
        return f"wiki_claim_id={ev.wiki_claim_id}"
    return f"wiki_page_id={ev.wiki_page_id}"


def _classify(conn: sqlite3.Connection, ev: DraftEvidenceIdentity) -> Optional[str]:
    """Return the invalidation reason for one evidence row, or ``None`` if current."""
    # A hook already recorded the deletion; trust the durable mark.
    if ev.source_deleted_at is not None:
        return SOURCE_DELETED

    resolver = _RESOLVERS.get(ev.source_kind)
    if resolver is None:
        # Unknown identity family cannot be proven current. Fail closed.
        return EVIDENCE_CHANGED

    live = resolver(conn, ev)
    if not live.exists:
        return SOURCE_DELETED
    if live.content_sha256 is not None:
        # The canonical content hash is the authoritative change signal. When
        # it matches, the source text is byte-identical and the evidence is
        # current by definition — a differing timestamp then means the row was
        # merely touched, not that what it says changed.
        #
        # The timestamps are deliberately NOT compared in that case. They come
        # from two different places: source_updated_at is captured at retrieval
        # time from index metadata, while live.updated_at is a database column,
        # so they can disagree in value and format while the content is
        # identical. Treating that as a change would raise a permanent,
        # non-waivable evidence_changed blocker on unmodified sources and make
        # Ready unreachable — the same false-positive failure mode that the
        # passage-versus-whole-source hash bug produced.
        return (
            EVIDENCE_CHANGED
            if live.content_sha256 != ev.source_content_sha256
            else None
        )
    # No derivable content hash: update metadata is the only available signal,
    # so fall back to it and fail closed on any difference.
    if (
        ev.source_updated_at is not None
        and live.updated_at is not None
        and str(live.updated_at) != str(ev.source_updated_at)
    ):
        return EVIDENCE_CHANGED
    return None


def check_evidence_freshness(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    page_size: int = EVIDENCE_PAGE_SIZE,
    max_evidence: int = MAX_EVIDENCE_PER_JOB,
) -> FreshnessResult:
    """Read-only SPEC 12.6 re-resolution of every evidence identity for ``job_id``.

    Walks the job's evidence in pages of ``page_size`` and compares each
    identity against the live source in the owning draft's vault scope. Writes
    nothing: :func:`enforce_evidence_freshness` owns the consequences.

    Args:
        conn: Open SQLite connection.
        job_id: The compile job whose evidence snapshot is being re-resolved.
        page_size: Rows fetched per page.
        max_evidence: Hard ceiling on rows examined. Exceeding it sets
            ``truncated`` and the result reports *not* current (fails closed).
    """
    store = DraftStore(conn)
    issues: list[EvidenceIssue] = []
    checked = 0
    offset = 0
    truncated = False
    while True:
        remaining = max_evidence - checked
        if remaining <= 0:
            # There is more evidence than the ceiling allows: check whether any
            # actually remains before declaring the pass truncated.
            truncated = bool(
                store.list_evidence_identities(job_id=job_id, limit=1, offset=offset)
            )
            break
        rows = store.list_evidence_identities(
            job_id=job_id, limit=min(page_size, remaining), offset=offset
        )
        if not rows:
            break
        for ev in rows:
            checked += 1
            reason = _classify(conn, ev)
            if reason is not None:
                issues.append(
                    EvidenceIssue(
                        evidence_id=ev.id,
                        label=ev.label,
                        source_kind=ev.source_kind,
                        reason=reason,
                        identity=_identity_label(ev),
                    )
                )
        offset += len(rows)
    return FreshnessResult(
        job_id=job_id, checked=checked, truncated=truncated, issues=tuple(issues)
    )


def enforce_evidence_freshness(
    conn: sqlite3.Connection,
    *,
    draft_id: int,
    revision_id: int,
    job_id: int,
    actor_user_id: Optional[int] = None,
    page_size: int = EVIDENCE_PAGE_SIZE,
    max_evidence: int = MAX_EVIDENCE_PER_JOB,
) -> FreshnessResult:
    """The SPEC 12.6 gate: re-resolve, and record the consequences if stale.

    Call this **before Final Assemble** and **again inside the Ready
    transaction**. The caller inspects :attr:`FreshnessResult.is_current` and
    refuses to store or approve the candidate when it is False.

    When the pass finds an issue this function, in one transaction:

    * stamps ``source_deleted_at`` on every evidence row whose source vanished
      (the row itself is kept — SPEC 5.6 history);
    * marks ``revision_id`` ``fact_status='invalidated'``;
    * opens one **non-waivable** blocker per reason
      (``evidence_changed`` / ``source_deleted``);
    * moves a ``ready`` draft back to ``needs_review``.

    The invalidation is **committed before returning**, even though the caller
    usually goes on to raise a 409 and roll back its own work: SPEC 12.6
    requires the stale state to be recorded, not lost with the refused request.
    A clean (current) result commits nothing.

    Args:
        conn: Open SQLite connection. May already be inside the caller's
            ``BEGIN IMMEDIATE`` (the Ready path is); this function joins that
            transaction rather than starting a competing one. Because a stale
            result commits, the caller must run this check **before** it makes
            any writes it might still want to abandon — in the Ready path that
            means before ``drafts`` is touched, which is where SPEC 12.6 puts
            it anyway.
        draft_id: Owning draft.
        revision_id: The current revision the candidate belongs to.
        job_id: The compile job whose evidence backs that revision.
        actor_user_id: Recorded on the audit event when a human triggered the
            check; ``None`` for automatic paths.
    """
    result = check_evidence_freshness(
        conn, job_id=job_id, page_size=page_size, max_evidence=max_evidence
    )
    if result.is_current:
        return result

    store = DraftStore(conn)
    if not conn.in_transaction:
        store._begin_immediate()  # noqa: SLF001 - store-owned transaction helper
    try:
        deleted_ids = [i.evidence_id for i in result.issues if i.reason == SOURCE_DELETED]
        if deleted_ids:
            store.mark_evidence_source_deleted(evidence_ids=deleted_ids, commit=False)
        for reason in result.reasons:
            store.apply_evidence_invalidation(
                draft_id=draft_id,
                revision_id=revision_id,
                job_id=job_id,
                reason=reason,
                evidence_ids=[i.evidence_id for i in result.issues if i.reason == reason],
                actor_user_id=actor_user_id,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info(
        "Draft evidence invalidated: draft_id=%d revision_id=%d job_id=%d "
        "reasons=%s evidence_count=%d",
        draft_id,
        revision_id,
        job_id,
        ",".join(result.reasons),
        len(result.issues),
    )
    return result


# ---------------------------------------------------------------------------
# Mutation hooks (SPEC 12.6) — best effort, never raise into the host operation
# ---------------------------------------------------------------------------


def _invalidate_for_source(
    conn: sqlite3.Connection,
    *,
    identity: str,
    source_id: int,
    new_content_sha256: Optional[str],
    actor_user_id: Optional[int] = None,
) -> int:
    """Indexed invalidation for one changed/deleted source.

    ``identity`` selects one of ``DraftStore.EVIDENCE_SOURCE_FILTERS``'s partial
    identity indexes. ``new_content_sha256=None`` means the source was deleted;
    otherwise it is the source's new canonical whole-source hash and only
    evidence whose snapshot hash differs is invalidated (so a no-op save is a
    no-op here, and a repeated hook is idempotent).

    Returns the number of drafts whose current revision was invalidated.
    """
    store = DraftStore(conn)
    deleted = new_content_sha256 is None

    # Collect affected evidence in pages, grouped by (draft, job).
    affected: dict[tuple[int, int], list[int]] = {}
    offset = 0
    while True:
        rows = store.list_evidence_identities_for_source(
            source_kind=identity,
            source_id=source_id,
            limit=EVIDENCE_PAGE_SIZE,
            offset=offset,
        )
        if not rows:
            break
        for ev in rows:
            if not deleted and ev.source_content_sha256 == new_content_sha256:
                continue
            affected.setdefault((ev.draft_id, ev.job_id), []).append(ev.id)
        offset += len(rows)
        if offset >= MAX_EVIDENCE_PER_JOB:
            break
    if not affected:
        return 0

    reason = SOURCE_DELETED if deleted else EVIDENCE_CHANGED
    invalidated = 0
    # SPEC 12.6 asks for the invalidation to land "in the same database
    # transaction where possible". When the host service is mid-transaction we
    # join it and let the host commit; only when there is no open transaction do
    # we own one. Beginning or committing under a host transaction would commit
    # or discard the host's own in-flight work.
    own_tx = not conn.in_transaction
    if own_tx:
        store._begin_immediate()  # noqa: SLF001 - store-owned transaction helper
    try:
        for (draft_id, job_id), evidence_ids in sorted(affected.items()):
            if deleted:
                store.mark_evidence_source_deleted(
                    evidence_ids=evidence_ids, commit=False
                )
            # Only the *current* revision produced by that job is still a live
            # claim about the world. Evidence behind a superseded revision is
            # history and is left alone (it is still marked, above).
            row = conn.execute(
                "SELECT id FROM draft_revisions "
                "WHERE draft_id = ? AND job_id = ? AND is_current = 1",
                (draft_id, job_id),
            ).fetchone()
            if row is None:
                continue
            if store.apply_evidence_invalidation(
                draft_id=draft_id,
                revision_id=int(row[0]),
                job_id=job_id,
                reason=reason,
                evidence_ids=evidence_ids,
                actor_user_id=actor_user_id,
            ):
                invalidated += 1
        if own_tx:
            conn.commit()
    except Exception:
        if own_tx:
            conn.rollback()
        raise
    return invalidated


def _safe_hook(
    conn: Any,
    *,
    identity: str,
    source_id: Optional[int],
    new_content_sha256: Optional[str],
) -> None:
    """Run an invalidation hook so it can only ever log.

    SPEC 12.6 wants these hooks in the source's own transaction where possible,
    but the invalidation must never be able to fail, slow, or roll back the host
    operation (a Wiki save, a KMS edit, a document delete). Every failure mode
    — missing Draft Room tables, a locked database, a closed connection — is
    swallowed here; the mandatory before-Assemble/Ready re-resolution and the
    startup reconciler are the backstops that make that safe.
    """
    if source_id is None:
        return
    try:
        count = _invalidate_for_source(
            conn,
            identity=identity,
            source_id=int(source_id),
            new_content_sha256=new_content_sha256,
        )
        if count:
            logger.info(
                "Draft evidence invalidation hook: identity=%s source_id=%d "
                "drafts_invalidated=%d",
                identity,
                int(source_id),
                count,
            )
    except Exception as exc:  # noqa: BLE001 - hook must never break the host op
        logger.warning(
            "Draft evidence invalidation hook failed (continuing): "
            "identity=%s source_id=%s: %s",
            identity,
            source_id,
            exc,
        )


def on_document_changed(
    conn: sqlite3.Connection,
    *,
    file_id: Optional[int],
    new_content_sha256: Optional[str] = None,
) -> None:
    """Document updated (pass its new ``files.file_hash``) or deleted (``None``)."""
    _safe_hook(
        conn,
        identity="document",
        source_id=file_id,
        new_content_sha256=new_content_sha256,
    )


def on_wiki_page_changed(
    conn: sqlite3.Connection,
    *,
    page_id: Optional[int],
    new_markdown: Optional[str] = None,
) -> None:
    """Wiki page updated (pass its new markdown) or deleted (``None``).

    Invalidates *every* evidence row carrying this ``wiki_page_id``, page-level
    and claim-level alike (SPEC 12.6).
    """
    _safe_hook(
        conn,
        identity="wiki_page",
        source_id=page_id,
        new_content_sha256=None if new_markdown is None else sha256_text(new_markdown),
    )


def on_wiki_claim_changed(
    conn: sqlite3.Connection,
    *,
    claim_id: Optional[int],
    new_claim_text: Optional[str] = None,
) -> None:
    """Wiki claim updated (pass its new text) or deleted (``None``)."""
    _safe_hook(
        conn,
        identity="wiki_claim",
        source_id=claim_id,
        new_content_sha256=(
            None if new_claim_text is None else sha256_text(new_claim_text)
        ),
    )


def on_kms_entry_changed(
    conn: sqlite3.Connection,
    *,
    entry_id: Optional[int],
    new_body: Optional[str] = None,
) -> None:
    """KMS entry updated (pass its new body) or deleted (``None``)."""
    _safe_hook(
        conn,
        identity="kms",
        source_id=entry_id,
        new_content_sha256=None if new_body is None else sha256_text(new_body),
    )


# ---------------------------------------------------------------------------
# Startup reconciler (SPEC 12.6)
# ---------------------------------------------------------------------------


def _reconcile_page(
    pool: Any, after_id: int, limit: int
) -> list[tuple[int, int, int, int, Optional[int]]]:
    conn = pool.get_connection()
    try:
        return DraftStore(conn).list_ready_drafts_page(after_id=after_id, limit=limit)
    finally:
        pool.release_connection(conn)


def _reconcile_one(
    pool: Any, draft_id: int, revision_id: int, job_id: int
) -> FreshnessResult:
    conn = pool.get_connection()
    try:
        return enforce_evidence_freshness(
            conn, draft_id=draft_id, revision_id=revision_id, job_id=job_id
        )
    finally:
        pool.release_connection(conn)


async def reconcile_ready_evidence(
    pool: Any,
    *,
    page_size: int = RECONCILE_PAGE_SIZE,
    max_drafts: int = RECONCILE_MAX_DRAFTS,
) -> ReconcileSummary:
    """Bounded, paginated re-resolution of every Ready draft's evidence.

    Runs at startup, **before HTTP traffic and the job processors**, so an
    out-of-band or database-only source deletion cannot leave a draft sitting in
    Ready on stale evidence indefinitely (SPEC 12.6).

    Bounds: at most ``max_drafts`` drafts, walked by keyset in pages of
    ``page_size``; each draft's own evidence pass is itself bounded by
    :data:`MAX_EVIDENCE_PER_JOB`. Reaching ``max_drafts`` sets ``truncated`` and
    is logged — the next startup resumes the sweep. Each page and each draft
    take a pooled connection and release it before the next ``await``, so no
    connection is ever held across one.

    Idempotent: a draft already moved to ``needs_review`` no longer matches the
    Ready page query, and repeated invalidation of the same revision reuses the
    existing open blocker rather than creating a second one.

    Never raises — a reconciler failure must not prevent startup.
    """
    scanned = 0
    invalidated = 0
    evidence_checked = 0
    truncated = False
    after_id = 0
    try:
        while scanned < max_drafts:
            limit = min(page_size, max_drafts - scanned)
            page = await asyncio.to_thread(_reconcile_page, pool, after_id, limit)
            if not page:
                break
            for draft_id, _owner_id, _vault_id, revision_id, job_id in page:
                after_id = draft_id
                scanned += 1
                if job_id is None:
                    # A manual revision has no evidence snapshot to re-resolve.
                    continue
                result = await asyncio.to_thread(
                    _reconcile_one, pool, draft_id, revision_id, int(job_id)
                )
                evidence_checked += result.checked
                if not result.is_current:
                    invalidated += 1
            if len(page) < limit:
                break
        else:
            truncated = True
    except Exception as exc:  # noqa: BLE001 - startup must not fail on this
        logger.warning(
            "Ready-evidence reconciler stopped early (continuing startup) "
            "after %d drafts: %s",
            scanned,
            exc,
        )
    logger.info(
        "Ready-evidence reconcile: drafts_scanned=%d drafts_invalidated=%d "
        "evidence_checked=%d truncated=%s",
        scanned,
        invalidated,
        evidence_checked,
        truncated,
    )
    return ReconcileSummary(
        drafts_scanned=scanned,
        drafts_invalidated=invalidated,
        evidence_checked=evidence_checked,
        truncated=truncated,
    )


# Re-exported so callers can stamp the same rule version on related records.
RULE_VERSION = EVIDENCE_INVALIDATION_RULE_VERSION
