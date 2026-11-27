# fix(backend): #1209 off-loop LLMClient sanitizer + #1199 single connection per organizations request

## What changed

- **#1209** — `backend/app/services/llm_client.py` (note: the sanitizer
  now runs on the asyncio DEFAULT thread pool, which it shares with
  handler SQL dispatch; pool CHECKOUTS stay on their dedicated executor;
  executor-occupancy isolation is routed to #1215 alongside the loop-stall
  bound, and cancellation abandons the worker — the pass runs to
  completion): both completion paths
  (`chat_completion` and the non-SSE branch of `chat_completion_stream`)
  now dispatch `_strip_thinking_content` through `asyncio.to_thread`, so
  the regex-heavy assistant sanitizer (five `re.sub` passes plus a
  line-walking wrapper stripper over the full provider response) runs on a
  worker thread instead of the event-loop thread. Returned content and
  provider-exact token accounting are unchanged; a frozen thread-identity
  spec (`backend/tests/test_llm_client_offloop_sanitizer.py`) pins both
  paths. NOTE: this fixes the thread-placement contract only — each
  regex pass still holds the GIL for its duration, so a single expensive
  pass can still stall the loop (adversarial input measured at 421s wall /
  421s max heartbeat gap off-thread); that residual is tracked in #1215.
- **#1199** — scope corrected to the code-verified double-hold class
  IN THIS FILE: all 18 `backend/app/api/routes/organizations.py` handlers
  migrated from a
  second standalone `pool.get_connection_async()` checkout to the
  request-scoped `db: sqlite3.Connection = Depends(get_db)` connection
  shared with the `require_role` auth chain (the pattern PR #1198 shipped
  for users.py). One pooled checkout and one release per request now holds
  across success, pre-auth denial, authenticated in-handler 403, and
  exception outcomes — pinned by the five-leg counting-pool spec
  (`backend/tests/test_p02c_orgs_pool_single_connection.py`). The issue's
  published AC1 (prompts.py count 0) and AC2 (service_accounts.py count 0)
  are REFUTED, not met: every handler in those files authenticates via
  `require_scope` (header-only — `backend/app/security/__init__.py:434-462`,
  no pool use anywhere in the dependency), and the pool checkout the issue's
  triage attributed to that chain at `security/__init__.py:501` actually
  lives inside `require_service_account` (:465+), which acquires AND
  releases inside the dependency — one connection per request there, no
  double-hold. The guardrail (0 violations at head, CI-wired) is the
  ongoing check for the corrected class. NOTE: the gate scans handler
  subtrees only, so it cannot see acquisitions in module-level helpers —
  a LIVE instance of the same class remains in the documents.py upload path
  (the handler holds a request-scoped connection via require_vault_permission
  while _do_upload takes a second one at documents.py:2884; pre-existing and
  unchanged by this PR), filed as Priority follow-up #1217.
- Nine direct-caller/race harness suites migrated to the `db=` call-site
  seam (distinct connections per simulated request preserved).
- New Quality-contracts guardrail `scripts/check_double_hold_routes.py`
  (fail-closed within its scanned lane on unclassified auth dependencies;
  C-DBLHOLD-1 double-hold, C-DBLHOLD-2 unknown-auth, C-DBLHOLD-3
  manual-release rules), wired into `ci.yml`, the `justfile`
  `quality-contracts` recipe, and six script-inventory mirrors (EIGHT surfaces in
  total, the ci.yml step included)
  (`AGENTS.md`, `docs/engineering/conventions.md`,
  `docs/engineering/testing.md`, `CONTRIBUTING.md`, the
  `ci-compatibility-audit` and `ci-fix-monitor` skills); contract tests (26) in
  `backend/tests/test_double_hold_guardrail.py`.

## Why

- #1209: the sanitizer executed on the event-loop thread in both
  completion paths (the thread-placement defect #1209 names; the same
  class was fixed for chat.py call sites under #1202 (whose PR #1211 has since
merged), and LLMClient was
  the deferred residual). Loop responsiveness under expensive regex
  passes is a separate, still-open property owned by #1215.
- #1199: the 18 organizations handlers held two pooled SQLite connections
  per request (the #205 hold-and-wait class), halving effective pool
  capacity on those routes; the 2026-11 deployment audit observed pool
  saturation on shallow calls (tracker #1148).

## Migration

No schema, config, or operator action. The draft-room
`upload_draft_input` detached-commit connection keeps its
independent-lifetime ownership by design (documented exception in the
guardrail).

## Caveats

- Direct Python callers of the migrated organizations handlers must now
  pass `db=` (HTTP contract unchanged).
- The guardrail fails closed within its scanned lane (handler-signature
  `Depends(require_*...)`/`Depends(get_current*...)` defaults): a new
  auth dependency declared there must be classified in the script's
  holding/non-holding sets. Router-level `APIRouter(dependencies=[...])`,
  `Annotated`-style params, `head`/`options` decorators, and acquisitions
  extracted into module-level helpers are outside the scan (documented in
  the script). The helper-extraction lane has a LIVE instance, tracked in
  #1217; the remaining lanes are known limitations with no live instance and
  no tracker.
