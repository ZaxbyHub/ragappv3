# Prompts/A-B API and citation-mode enforcement: no more created_by token leaks, GET-route CSRF, phantom-citation satisfaction, or A/B variant drops (#709)

## What changed

### Admin tokens no longer persist in audit columns
- `POST /api/prompts` now records a non-secret scope fingerprint
  (`admin_scope_fingerprint` in `backend/app/security.py`, the same formula
  the service-accounts route already used) instead of the verified token,
  which previously landed in the durable `prompt_versions.created_by` column
  and was echoed back by three prompts GET routes.
- The same class is fixed at the admin audit surface: `/admin/toggles` and
  `/admin/maintenance` now write the fingerprint into
  `audit_toggle_log.user_id` (and its HMAC input) instead of the raw token.
  Historical audit rows still hold the token and are deliberately NOT
  rewritten — the row's `hmac_sha256` covers `user_id`, so rewriting them
  would break audit-integrity verification. Backups created before this
  change also retain old rows; treat historical backups as secret-bearing.
- New idempotent startup migration `migrate_redact_prompt_created_by`
  (`backend/app/models/database.py`) rewrites legacy
  `prompt_versions.created_by` rows and journals start/succeeded outcomes so
  operators can prove the scrub ran. Detection is format-based: the pre-fix
  route was the only writer, so any value that is neither NULL/empty nor a
  16-hex fingerprint is token material — including rows written under
  previously ROTATED tokens (the old "unknowable after rotation" limitation
  does not apply to this column). Known edge: a hand-configured admin token
  that is itself exactly 16 lowercase hex chars would be indistinguishable
  from a fingerprint; the configured generator emits 64-char urlsafe tokens.

### Read-only prompts GET routes no longer require the CSRF handshake
- `GET /api/prompts`, `GET /api/prompts/active`, `GET /api/prompts/{version}`,
  and `GET /api/prompts/ab-experiments` no longer depend on `csrf_protect`;
  header-token API clients with a valid `admin:config` bearer token can read
  them without the double-submit cookie/header handshake (which
  `docs/engineering/conventions.md` scopes to mutations). All mutating
  prompts routes keep CSRF.
- Same defect class, fixed alongside: `GET /documents/reindex/jobs/{job_id}`
  also dropped its CSRF dependency (its sibling list route never had one),
  so reindex job-status polling no longer needs the handshake either. All
  four mutating prompts routes keep it (structurally pinned).
- A repo-wide structural guardrail (`backend/tests/test_c04_csrf_scope_guardrail.py`)
  now fails if any GET route on ANY module-level `APIRouter` under
  `app.api.routes` — including secondary routers — regains a `csrf_protect`
  dependency. Limit: dependencies attached only at app-assembly time
  (`include_router(dependencies=[...])`) are not scanned; none exist.

### Prompt create warns when content drops the citation/security contract
- `CreatePromptVersionResponse` gains an optional `warning` field. Creating a
  version whose content lacks the `[S#]/[W#]/[K#]/[M#]` label vocabulary or
  the SECURITY BOUNDARY untrusted-tag directive returns a non-empty warning
  naming what is absent (content is still accepted). The check is
  presence-based: one label of any family satisfies the vocabulary half, and
  the boundary half is a substring test — treat the warning as a hint, not a
  verification.

### `citation_mode="required"` no longer reports phantom labels as satisfied
- Standard-path enforcement (`backend/app/services/rag_engine.py`) validates
  label-set membership for every citation family (S/M/W/K) against the
  labels actually offered: a phantom `[M9]` with no memories, `[W7]` past
  the wiki evidence, or `[S99]` past the source count reports
  `missing_citations` with the offending labels named in `detail`, and a
  valid citation beside a phantom still flips the answer to
  `missing_citations`. The precondition became "any citable evidence was
  offered" (documents, memories, wiki, or KMS) — with zero citable evidence
  the status stays `satisfied` (nothing for citations to be required
  against), as before this change.
- "Offered" labels are the full retrieval lists, matching the pre-existing
  positional labeling; prompt-budget-shed items keep their labels in the
  offered sets, and memories with empty content keep their label slots (so
  an `M#` the prompt never rendered can count as offered) — the same
  approximation `_build_done_message` has always used.
- The agentic path enforces S-family membership only (pre-existing); the
  in-code comment no longer claims parity.

### Prompt-worded refusals are no longer flagged as unverifiable claims
- The default prompt now prescribes the exact refusal sentence
  "This information is not available in the provided context."
  (`PROMPT_REFUSAL_SENTENCE`, shared between `prompt_builder` and
  `citation_validator`), and the refusal detector's phrase list recognizes
  the "provided context"/"provided documents" phrasings. Caveat: the refusal
  veto is whole-content (pre-existing shape) — ANY answer containing a
  refusal phrase, including a partial answer that also makes claims, skips
  unverifiable-claim scanning for the whole answer.

### `citation_mode` no longer depends on the prompt containing one exact constant
- `citation_mode="disabled"` now strips the citation instruction when
  present AND appends an explicit disable directive for every prompt chain,
  so a stored custom override that lacks the exact `CITATION_INSTRUCTION`
  constant still visibly changes the system prompt (previously a silent
  no-op). Note for stored verbatim copies of the pre-change default prompt:
  they carry the old instruction wording, which the strip cannot match, so
  disabled mode now appends the directive next to the retained (contradicting)
  citation text; re-save such versions to pick up the new constant.
  `citation_mode="required"` always strengthens regardless of the constant.

### A/B assignment survives exposure-write and variant-lookup failures
- `RAGEngine._resolve_prompt_with_ab_sync` records the assigned variant
  before any telemetry write or content lookup; a failed exposure write
  (e.g. sqlite writer-lock timeout) or a failed variant content lookup logs
  a warning and the assignment metadata (`ab_variant`) is still reported.
  On lookup failure the 3.5 effective prompt content is served and
  `prompt_version` honestly names the version that actually served; the
  exposure count for that subject is recorded against the assignment. Pure
  telemetryWrite failures serve the challenger content in full.

### CI infrastructure
- `scripts/check_secretscan.py`'s on-disk walk now tolerates files and
  directories that vanish mid-scan (a pytest-xdist race with the frontend
  coverage gate's `npm run` could previously crash the scan with
  `FileNotFoundError`). `backend/security/bandit-baseline.json` is
  re-anchored for pure line drift from this change (131 findings, identity
  multiset unchanged; only `line_number` moved).

## Operator impact
- After upgrade, the startup migration rewrites legacy token-bearing
  `prompt_versions.created_by` rows (journalled; logged count at INFO).
  API clients that previously performed the CSRF handshake on the prompts
  GETs or the reindex job-status GET can drop it. Required-mode enforcement
  is stricter: answers citing labels that were never offered now report
  `missing_citations` naming them. The admin audit trail now records the
  scope fingerprint instead of the token; historical audit rows (and
  pre-change backups) still contain token material.

## Known limitations
- Historical `audit_toggle_log` rows retain the raw token (HMAC integrity
  prevents in-place redaction); pre-change backups do too.
- A 16-hex-shaped legacy admin token would evade the format-based
  `created_by` redaction (generator tokens are 64-char urlsafe).
- "Offered" citation labels are the full retrieval lists (see above).
- The refusal veto is whole-content (see above).
