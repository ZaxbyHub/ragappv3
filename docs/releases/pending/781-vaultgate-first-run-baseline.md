# Workstream M PR 1: fresh-install first-run baseline + shared VaultGate (Issue #781)

## What changed

### Frontend

- **`frontend/src/components/vault/VaultGate.tsx` (new)** — the shared
  control for vault-gated null states: the `VaultSelector` plus an
  "Open Vaults" action that navigates to `/vaults` on click (never on
  mount), with an optional "why you're seeing this" reason line. KMS and
  Wiki adopt the same component in Workstream M PR 2 (#782).
- **`frontend/src/pages/MemoryPage.tsx`** — the null-vault branch
  ("All Vaults", the default for a fresh browser) now renders the selector
  its empty state tells the user to use, via `VaultGate`, under the same
  "Memory" title as the vault-selected branch (was "Memories" with no
  selector anywhere on the branch). `MemoryPageContent` — and its memory
  queries — still never mounts without a vault.
- **`frontend/src/pages/DocumentsPage.tsx`** — the "Select a vault to view
  documents" empty state (vaults exist, none selected) now carries
  `VaultGate` inline instead of copy alone. The "No vaults available"
  state and the action-time permission toasts are unchanged.
- **`frontend/src/hooks/useSendMessage.ts`** — a send with no vault
  selected resolves its vault at the point of use: with exactly one
  accessible vault the send proceeds in that vault (session AND stream
  are both scoped to it — including follow-up messages in the
  auto-created session, since the backend rejects a null vault_id for
  non-admins) without overwriting the persisted "All Vaults" selection
  used by Documents/Search scoping; with zero or multiple accessible
  vaults the send is still gated, but the message now names the
  constraint and the control ("Pick a vault to chat in — choose one from
  the vault selector, or create a vault.") instead of the vault-agnostic
  "Please select a vault before starting a chat." that contradicted the
  composer's own "All Vaults" affordance.

### E2E harness (issue #781 / UI-ENH-07 measurement, reused by PR 2)

- **`frontend/e2e/first-run-baseline.m01.spec.ts` (new)** — the
  fresh-install walkthrough: from a clean context (no persisted
  `kv_active_vault_id`) it walks login → Memory (no vault selected) →
  vault creation → documents → first chat send from the "All Vaults"
  composer → recovery → first cited answer, counting every user action
  and recording every dead end. It writes
  `frontend/e2e/test-results/first-run-baseline-record.json`
  (`{action_count, dead_ends, completed}`) BEFORE asserting
  `dead_ends` is empty, and fails closed when the stub backend is
  unreachable, login fails, or a page under test never renders — a
  degraded run cannot produce a vacuously empty record.
  - Pre-fix measurement (base tree + harness): `action_count: 15`,
    `dead_ends`: the Memory no-selector dead end (title "Memories",
    selector count 0) and the Chat vault-contradiction dead end (exact
    old copy). Post-fix: `action_count: 15`, `dead_ends: []` (see the
    PR body for both records and the `completed[]` trail).
  - Known limitation (identical pre- and post-fix): the upload step
    records-and-continues as `documents-page-unavailable` because the
    dropzone's `<input type="file">` renders with a 0×0 box (never
    "visible" to Playwright); `setInputFiles`-based upload requires a
    spec change that is frozen with this checkpoint, so both runs
    compare like-for-like at 15 actions.
- **`frontend/e2e/stub-backend.mjs`** — additive routes the walkthrough
  needs (`POST /api/vaults`, `GET /api/documents`, `GET
  /api/documents/stats`, multipart `POST /api/documents`) and an
  env-driven listen port (`E2E_STUB_PORT`, default 9090).
- **`frontend/e2e/playwright.config.ts` / `frontend/vite.paths.ts`** —
  the stub webServer URL and the dev/preview API proxy target read
  `E2E_STUB_PORT` (default 9090; unset — CI and dev — is byte-identical
  to before). The config also bounds every un-timed wait
  (`actionTimeout` 20s / `navigationTimeout` 30s): the app polls
  `/api/health`, so an unbounded `networkidle` wait can never settle.

### Tests

- Frozen acceptance checks (`.m01.` fixtures): MemoryPage null-branch
  selector + title, useSendMessage All-Vaults first send, VaultGate
  contract, DocumentsPage inline selector — all RED at the base commit,
  GREEN at this PR head (see the issue's checkpoint anchor receipts).
- Updated defect pins (sanctioned by the issue): `useSendMessage.test.ts`
  pins the new constraint message and adds first-send resolution pins
  (session + stream both scoped to the sole vault; no store/localStorage
  write; multi-vault keeps the gate); `MemoryPage.test.tsx` pins that
  the null branch renders the gate's selector while `MemoryPageContent`
  stays unmounted (its `EmptyState` mock now models the real ReactNode
  `action` contract and renders under a router).
- New guardrail `frontend/src/tests/vaultGateSites.781.guardrail.test.tsx`:
  both in-scope null-branch surfaces must render the shared VaultGate
  (selector + click-only navigation; the Memory site specifically uses
  VaultGate, not a re-implementation) — demonstrated RED on the base
  tree.
