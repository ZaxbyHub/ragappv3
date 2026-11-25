# Wiki compiler extraction policy, unified review queue, wiki UI (issues #1161, #1162, #1163)

## Summary

- **Compiler extraction policy (#1162, approved by the owner's November 18, 2026
  decision):** a successful compile whose extraction yields zero qualifying items
  (no acronyms, no role claims with a subject, no procedures, no substantive
  curator output) now creates NO wiki page, NO file association, and NO KMS
  document entry — the raw source is untouched. A first-seen unqualifying
  document creates nothing; an unqualifying RECOMPILE of existing records keeps
  the non-destructive lifecycle (claims retired by the supersede pass, every
  associated page refreshed and marked `stale`, existing KMS entry refreshed —
  nothing deleted). The wiki ingest worker is the single qualification owner and
  writes both derived records when they are enabled (`kms_enabled` +
  `kms_compile_on_ingest` stay authoritative); the independent KMS job uses the
  pure deterministic criterion and converges through the idempotent upsert.
  Numbered procedures in content blocks become procedure pages (steps verbatim);
  licence/copyright/change-record/TOC blocks are excluded from KMS summaries;
  TOC blocks are filtered from retrievable chunks at final-output granularity in
  both selectable chunkers; curator and deterministic candidates without a
  subject entity are rejected (`missing_subject`). A typed
  `UnsupportedContentTypeError` distinguishes binary refusal (clean skip) from a
  parser crash on a supported type (job failure).
- **Unified review queue (#1163):** `GET /api/review/queue` merges wiki pages
  awaiting review (`needs_review`) and compiled KMS entries (`draft`,
  `source_type=document`) into one list for write-access callers, each beside
  the source quote it rests on; `POST /api/review/queue/batch` approves
  (verified/published) or rejects (archived) every selected item or none —
  validation and mutations run inside one transaction through store lifecycle
  operations (version snapshots and activity logs preserved). New page at
  `/review-queue` with kind/status filters and batch actions, registered in the
  desktop rail, mobile drawer, and route table.
- **Wiki UI (#1161):** opening a wiki page from the list puts the page's
  identifier in the address (`?page=<id|slug>`, the existing deep-link
  contract; the URL param is the single owner — one detail fetch per value,
  Back closes); the wiki list gains a Mark Reviewed bulk action
  (needs_review -> verified), per-category totals over the loaded window, and
  updated-time labels; the Jobs view keeps the lint result available after
  every lint run (persisted server-side findings with status counts, origin,
  filename, and time labels — `GET /wiki/lint` now joins a `source_filename`
  over the finding's related claims/pages).

## Cleanup of existing empty derived records (planning only)

The owner decision authorizes PLANNING cleanup of existing empty derived
records; no live cleanup ships in this PR. Plan: (1) per-vault, per-source-
revision inventory (derived-record id, provenance, last content change, review
state, inbound citations, curator output, extraction result; unknowns counted
separately); (2) eligibility ONLY for machine-generated records with a
verified empty extraction, no substantive body or claims, and no human edits,
review, or inbound references — ambiguous, edited, reviewed, and cited rows
are retained; (3) owner preview with counts by disposition, tested against a
confirmed-empty record, a failed extraction, substantive curator-only output,
a manual edit, a reviewed page, a same-name source, and a cited page — only
the confirmed eligible row enters the proposed set; (4) before any future
execution, the preview is bound to a revision, eligibility rechecked
transactionally, a restorable manifest preserved, and interruption/retry
tested in a disposable vault.

## Test plan

- `cd backend && python -m pytest tests/test_qualification_approved_policies.py tests/test_audit1115_h2_compile.py tests/test_audit1115_h2_companions.py tests/test_audit1115_h3_review.py -q -p no:randomly -p no:cacheprovider`
- `cd backend && python -m pytest tests/test_wiki_compiler.py tests/test_wiki_compile_processor.py tests/test_issue515_wiki_compiler.py tests/test_o03_kms_compile_vault_status.py tests/test_o03b_negative_status_pins.py tests/test_wiki_routes.py -q -p no:randomly -p no:cacheprovider`
- `cd frontend && NO_COLOR=1 npx vitest run src/pages/WikiPage.address.test.tsx src/pages/WikiPageList.review.test.tsx src/pages/WikiJobsPanel.lint-result.test.tsx src/pages/ReviewQueuePage.test.tsx`
- `cd frontend && npm run typecheck && npm run lint -- --max-warnings 0 && npm run build`
- `python ../scripts/run_bandit.py` (baseline re-anchored, count flat)
