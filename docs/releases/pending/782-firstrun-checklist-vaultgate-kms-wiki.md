# Workstream M PR 2: first-run checklist with server-recorded milestones + VaultGate on KMS/Wiki (Issue #782)

## What changed

### Backend

- **`backend/app/api/routes/onboarding.py` (new)** — the first-run
  milestone surface. `GET /api/onboarding/milestones` derives
  `vault_created` (vaults the user owns), `upload_indexed` (a file with
  status `indexed` in an owned vault), and `first_question_asked` (a
  user-role chat message in the user's session) live from the existing
  tables, plus `first_citation_opened` from the new per-user state table,
  and reports `show_checklist = not dismissed and not all-four-complete`.
  `POST /api/onboarding/milestones/citation-opened` persists the one
  milestone nothing else records (audit review correction C4);
  `POST /api/onboarding/milestones/dismiss` persists an explicit
  dismissal. Both writes are auth + CSRF + rate limited and idempotent
  (first timestamp wins).
- **`backend/app/models/database.py`** — new
  `user_onboarding_state(user_id PK, citation_opened_at,
  checklist_dismissed_at, updated_at)` table via the journaled
  `migrate_add_user_onboarding_state` (CREATE IF NOT EXISTS only — no
  ALTERs, no backfill; SCHEMA double-definition convention).
- **`backend/app/main.py`** — router registration under `/api`.

### Frontend

- **`frontend/src/components/onboarding/FirstRunChecklist.tsx` (new)** —
  the dismissible checklist: four milestones rendered from the server
  payload (`data-testid="checklist-milestone"` with a discriminating
  `data-done`), a Dismiss control, and a ~3s poll while shown so
  milestones flip live. Self-fetching and fail-open: renders null while
  pending, on fetch error, and when the server hides it — a broken
  milestones endpoint can never break the shell, and no live region is
  added (PageShell's status-absent tests stay green).
- **`frontend/src/components/layout/PageShell.tsx`** — mounts the
  checklist in the shell's `<main>` (sibling of UnconfiguredChatBanner),
  so it is on screen wherever the user lands after Setup finishes
  (`navigate("/")` → `/documents`) without interrupting the two-step
  wizard, and stays visible across routes (the four milestones happen on
  /vaults, /documents and /chat).
- **`frontend/src/lib/api/onboarding.ts` (new)** — the milestone client
  (`getOnboardingMilestones` / `markCitationOpened` / `dismissChecklist`
  plus a once-per-page-life `reportCitationOpened` helper that latches
  only on 2xx). Direct-import module (not re-exported through the
  `@/lib/api` barrel).
- **`frontend/src/components/vault/VaultGate.tsx`** — router tolerance
  via a named `useOptionalNavigate` hook (the WikiPage
  `useOptionalSearchParams` shape): the gate now also renders in
  router-less mounts; the Open Vaults click stays exactly
  `navigate("/vaults")`.
- **`frontend/src/pages/KMSPage.tsx` / `frontend/src/pages/WikiPage.tsx`**
  — the two remaining page-level null-vault gates render the shared
  VaultGate inside their empty states (KMS's raw `<p>` becomes the same
  EmptyState shape Documents/Memory use; Wiki's EmptyState gains the
  action). Header selectors and all action-time permission toasts are
  untouched.
- **`frontend/src/components/chat/AssistantMessage.tsx` /
  `RightPane.tsx`** — opening a citation (source card, chip, or
  evidence-pane list item) fires the milestone report
  (fire-and-forget, failure-isolated).

### E2E harness (issue Verification + the #781-carried FB-003)

- **`frontend/e2e/stub-backend.mjs`** — onboarding milestone state
  (stub-default OFF), the three milestone routes, test-only
  `/_e2e/onboarding` and `/_e2e/setup-mode` control routes, a
  login-mirroring `POST /api/auth/register` (session cookies + refresh
  eligibility), and milestone flips wired to vault-create, document
  upload and the first streamed user message.
- **`frontend/e2e/first-run-baseline.m01.spec.ts`** — the walkthrough now
  drives the REAL two-step setup wizard (setup-mode via the control
  route; "Skip setup" finishes without a settings write) and asserts the
  checklist appears on the post-Setup landing surface with all four
  milestones incomplete. The upload step is REAL now (FB-003): the
  walkthrough selects a writable vault via the header selector and sets
  files on the (intentionally hidden) dropzone input directly, replacing
  the base record's `documents-page-unavailable` degradation marker.
  Each of the four milestone flips is hard-asserted (DOM polls for the
  first three; the fourth against the stub state because the same
  response that reveals completion also hides the checklist), and the
  record gains `milestone_flips` plus an action-count accounting note
  (base 15 → ~20: setup +5, login −3 because register authenticates,
  upload +3 — the shared journey's per-step counts are unchanged).
  afterAll resets the stub control state so the other shared-stub specs
  (chat-smoke, chat-width-budget) are order-independent.

### Tests

- Frozen acceptance checks (trace 782): `KMSPage.m02.test.tsx`,
  `WikiPage.m02.test.tsx`, `backend/tests/test_m02_onboarding_milestones.py`
  (3 route tests), `FirstRunChecklist.m02.test.tsx`.
- New unfrozen pins: `VaultGate.m02.test.tsx` (real-router bare-render
  tolerance + navigation), `FirstRunChecklist.m02b.test.tsx` (fail-open
  shell contract: null pending/error/hidden, no live region, interval
  cleanup, poll flip, dismiss), `onboarding.m02.test.ts` (client paths +
  once-guard semantics), `citationReporting.m02.test.tsx` (both chat
  surfaces fire the report), `SetupLanding.m02.test.tsx` (Setup finish →
  `/` → `/documents` → shell checklist, the AC7 landing pin),
  `test_m02_onboarding_migration.py` (real migration + journal +
  idempotence), `test_m02_onboarding_derivation.py` (one source row → one
  flag census, strict `indexed` vs `partial` semantics, NULL-user
  sessions excluded).
- Guardrail extended (`vaultGateSites.781.guardrail.test.tsx`): KMS and
  Wiki null branches must render the gate selector inline with the
  Open Vaults action and never navigate on mount (class C17).
- Determinism mocks for the real-PageShell suites (`PageShell.test.tsx`,
  `issue515-memory.test.tsx`, `command-palette.issue258.test.tsx`).

## Semantics notes (documented decisions)

- `upload_indexed` is strict `status = 'indexed'` (the acceptance
  criterion's wording). A scan/sync-path partial success lands `indexed`
  + `partial_embeddings=1` and counts; an upload/reindex-path partial
  lands `partial` and does not.
- The upload→user tie is vault ownership: a member uploading into a
  vault someone else owns credits the owner. Exact for the fresh-install
  walkthrough this checklist serves.
- Pre-existing vaults with NULL `owner_id` (pre-#845-era databases)
  credit no one — their owners demonstrably completed onboarding before
  the feature existed.
- `MaintenanceSettings.tsx` and `SetupPage.tsx` are deliberately
  unchanged: the maintenance toasts are action-time guards behind
  disabled buttons with an existing inline hint (the same class PR 1
  excluded), and the shell mount makes every post-finish landing surface
  the checklist without touching the wizard.
