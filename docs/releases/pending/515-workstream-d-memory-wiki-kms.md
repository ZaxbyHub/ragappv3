# fix(knowledge): close Memory, Wiki and KMS correctness findings (issue #515)

## What changed

Consolidated correctness vertical for the curated-knowledge surface (issue #515,
workstream D PR 1 of 3). 36 owned audit findings closed with 45 executable acceptance
checks (43 RED→GREEN, 2 preserving), all frozen before the fix landed:

- **Wiki store/jobs**: version-history id tie-break; conditional terminal-state
  predicates for complete/cancel/retry in both wiki and KMS job stores (a concurrent
  cancel can no longer be overwritten, and terminal jobs cannot be resurrected);
  wiki search now filters and orders in SQL before the cap; a shared Unicode-aware
  FTS tokenizer (`backend/app/services/fts_query.py`) replaces raw MATCH on every
  ordinary search path (hyphenated "Model-X" and CJK queries work on wiki, KMS and
  documents; punctuation-only input stays safe).
- **Wiki compiler/curator**: superseded memory claims reactivate on unchanged-content
  recompile; document pages refresh compiler-owned content on recompile (history
  preserved); claim reuse reloads sources by claim id (no duplicate attachments);
  document pages are file-identity-based with collision-safe slugs; accepted curator
  claim+source persistence is explicitly transactional; changed-number quotes no
  longer auto-verify; dedup keys include claim text; curator `open_questions` are
  retained in results.
- **Routes**: chat compile persistence honors `wiki_enabled`/`wiki_compile_on_query`
  on the enqueue path and re-checks at worker dispatch; delete-all persists wiki
  invalidation atomically with the file deletes; plain-document wiki pages report
  `compiled` with their page; explicit `parent_id: null` clears a page parent; memory
  tags reject non-string JSON elements; memory updates distinguish explicit-null
  clears from omission; whitespace-only memory content is rejected; `/kms/search`
  no longer requires vector readiness; memory embeddings are revision-guarded so a
  late old-content vector cannot overwrite newer content; backfill counts
  failures/skips truthfully; zero importance round-trips; no-op memory saves keep
  wiki claims active.
- **Search quality**: match-centered `snippet()` excerpts for documents ranked search
  (late body matches and per-metadata-column selection) and KMS retrieval evidence.
- **Frontend**: generation-guarded wiki/KMS list+detail fetches; 409 conflicts keep
  the edit dialog and draft; job-completion refreshes retain active filters; wiki and
  KMS lists gain `total` + Load more; lint dismissal persists via fingerprint-based
  suppression (dismissed findings are not resurrected); `/wiki?page=` deep links open
  the requested page; detail sections render the real API contracts (dates, filenames,
  backlink source titles); vault-wide claims tab; claim source chips link to origins;
  "How knowledge works" lifecycle panel; distinct `skipped` document badge and
  claimless-page empty state; KMS detail a11y names; KMS body renders as Markdown;
  Ctrl+Enter guarded to a single submit; global "Search across everything" surface
  with type filters over documents, chats, wiki and KMS (`GET /api/search/unified`).

## Why

Closes ZaxbyHub/ragappv3#515 (36 owned findings + LIVE-05/LIVE-08/DEEP-D-02/
DEEP-C-01/PRODUCT-ENH-07/PRODUCT-ENH-11 and legacy-07 verification). Each repair was
driven by a pre-fix RED acceptance check; the full protocol, evidence, and check
ledger are recorded in the issue trace.

## Known limitations

- Document-page refresh is unconditional on recompile (the schema has no
  manual-edit flag); prior content stays recoverable in `wiki_page_versions`.
- Legacy colliding same-name document pages are re-homed to distinct pages on their
  next recompile; until then an existing collision keeps its shared page (no data
  loss, no migration — C1 owns central migrations).
- KMS update-route explicit-null clears (`kms.py`) and wiki claim-field null clears
  (`wiki.py` claim update) share the exclude_none pattern but are outside #515's
  owned findings; documented for their owners in the trace's recurrence sweep.
- CJK FTS matching is prefix-based (unicode61 indexes contiguous CJK runs as single
  tokens) — mid-token CJK substrings do not match; inherent to the SQLite tokenizer.

## Rollout / rollback

No schema migration; additive code only. Rollback = revert the PR (baseline re-anchor
ships with it). Flags and operator settings unchanged; the curator remains off unless
already configured.
