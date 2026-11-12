# fix(wiki): verified curator claims activate matches, user lint dispositions survive reconciles, lint runs leave the event loop alone

Collated issues #929 + #932 + #935 (the #791 wiki-lint follow-up family).
One PR authorized by the session owner 2026-11-11.

## What changed

**Curator claims promote the matches they verify (wiki_compiler.py — #929)**
- In `active_if_verified` mode, a quote-verified accepted claim whose text
  (exact or normalized) matches an existing `unverified` claim now promotes
  that claim to `active` and lifts its `source_type`/`confidence`, the same
  column lift the deterministic `_find_or_create_claim` twin performs. The
  promotion is commit-free inside the claim-pair savepoint and predicated on
  `status = 'unverified'`; a failed source attach rolls the promotion back
  with the pair. `source_type` lifts to `document` only when the curator's
  evidence is document-backed (`file_id` present, the ingest trigger); on the
  manual/query triggers a matched claim keeps its own source_type, so
  memory-sourced provenance is never relabeled. Previously the matched claim stayed `unverified` forever —
  quote-verified knowledge the curator had just confirmed never activated.
  Draft-mode (`needs_review`) candidates never promote (unchanged boundary,
  now pinned).
- The superseded→active reactivation half of the parity contract shipped
  earlier with the #794/#795 feedback rounds; its behavior is unchanged and
  now pinned alongside.

**User lint dispositions survive concurrent reconciles (wiki_store.py — #932)**
- `upsert_lint_findings` now reads its snapshot INSIDE the `BEGIN IMMEDIATE`
  write transaction, so the snapshot + decisions + writes are one serialized
  unit instead of decisions computed from a pre-lock snapshot.
- The resolve-pass UPDATE carries `AND status = 'open'` and the rule-3
  reopen UPDATE carries `AND status = 'auto_resolved'` — a user disposition
  that lands in the (now much smaller) window can no longer be overwritten
  to `auto_resolved` or resurrected as `open`. A reopen that finds the row
  no longer `auto_resolved` suppresses the fingerprint instead of inserting
  a duplicate open row.
- Single-process behavior is unchanged (the predicates match the snapshot
  state in the serial case). Latent until the deployment runs multiple
  workers/threads on one SQLite file, which is exactly when it bit.

**Lint runs stop blocking every other request (routes/wiki.py — #935 item 1)**
- `POST /wiki/lint/run` dispatches the synchronous `WikiLinter.run_lint`
  scan onto a dedicated bounded executor (max_workers=1, wiki-lint thread
  prefix) via `run_in_executor`, so a long lint no longer stalls the event
  loop for the whole process — and a burst of concurrent lint runs cannot
  occupy the shared default executor that carries the per-request auth
  read (review round 1, PRR-001).

**Curator lint details are capped at 500 chars (wiki_curator.py — #935 item 2)**
- Every LLM-derived string stored in lint `details` — `claim_text` (three
  sites), model-cited `chunk_id` (two sites), and the four contradiction
  fields `claim_a`/`claim_b`/`reason`/`source_quote` — is bounded by the
  documented `_DETAIL_TEXT_MAX_CHARS = 500` (matching the pre-existing
  `attempted_quote` cap, which now uses the same constant). Values pass
  through safe `str()` normalization first, so a schema-drifting model
  payload cannot TypeError the slice. A hostile or runaway model can no
  longer push unbounded text into a column served to every lint-panel
  reader.

**SAST baseline diffs are content-keyed multisets (check_sast_baseline.py — #935 item 3)**
- `_diff_added_keys` keys findings on `(test_id, filename, issue_text)`
  with per-identity occurrence counts instead of `filename:line_number`.
  A pure line drift (an unrelated edit shifting scanned files) no longer
  prints ~113 "Newly-suppressed IDs" at a zero count delta; a genuine
  content add — including a duplicate occurrence of an existing finding —
  still reports. The separate `run_bandit.py` gate identity is unchanged.
- The PR also regenerates `backend/security/bandit-baseline.json`
  (line-number re-anchor only: 128 findings before and after, identical
  (test_id, filename, issue_text) multiset, zero new suppressions) —
  per `backend/security/README.md` this is the required disclosure.

**Unchanged, now pinned (was already fixed earlier)**
- `update_lint_finding` still rejects the system-only `auto_resolved`
  status for user calls and accepts exactly resolved/dismissed/acknowledged
  (#929 AC4).
- `WikiLintPanel` still shows a status label and offers Resolve/Dismiss
  only on open findings (#935 item 4, shipped with the #930 feedback
  round).

## Test plan

- backend/tests/test_o06_curator_claim_parity.py (C1/C3/C4 + normalized-match
  and attach-rollback arms)
- backend/tests/test_o06b_lint_reconcile_race.py (C5/C6/C7/C8 + AC6/AC7
  predicate source pins)
- backend/tests/test_o06c_wiki_lint_route_offloop.py (C9)
- backend/tests/test_o06d_curator_detail_caps.py (C10, incl. chunk_id legs)
- backend/tests/test_o06e_sast_diff_multiset_key.py (C11, incl.
  duplicate-occurrence multiset leg)
- frontend/src/pages/WikiLintPanel.test.tsx (C12, pre-existing)
- Blast radius: backend/tests/test_o01_*, test_o02_*, test_o05*, test_wiki_*,
  test_issue515_wiki_curator, test_settings_curator.
