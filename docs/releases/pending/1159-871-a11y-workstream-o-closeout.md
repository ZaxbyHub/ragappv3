# fix(frontend,a11y): Workstream O closeout — focus floor, theme color-scheme, Label-in-Name, axe 4.14 gate

## What changed

Closes #1159 (accessible focus and controls) and #871 (accessibility gate
extensions) — the final two issues of Workstream O, collated by explicit user
authorization.

- **Global keyboard-focus floor (#1159 AC4)**: an unlayered `:focus-visible`
  rule in `frontend/src/index.css` draws a 2px `--ring` outline (offset 2px)
  on every focused control. Unlayered normal declarations outrank Tailwind's
  `@layer utilities`, so controls whose utilities opt out via
  `focus-visible:outline-hidden` are re-armed; components that pair a ring
  keep both indicators. At base, computed `outline-style` was `none` on every
  probed control.
- **color-scheme per theme (#1159 AC1)**: `:root` light, `.dark` dark,
  `.high-contrast` light — native UA controls and scrollbars now follow the
  active palette.
- **State-aware rail toggle (#1159 AC2)**: the sidebar toggle announces
  "Collapse sidebar"/"Expand sidebar" by state (+ `aria-expanded`), mirroring
  the ChatShell/FolderTree idiom.
- **Session-identifying revoke names (#1159 AC3)**: each session's revoke
  control on `/profile` is named `Revoke session <id>: <device> (<ip>)` —
  unique even for same-browser-same-IP siblings.
- **axe-core 4.13.0 -> 4.14.1 (#871 AC5, absorbs the #1060 AC2 held bump)**
  with a load-time version gate (`frontend/e2e/axe-version-gate.mjs`) wired
  into BOTH the FROZEN #778 matrix setup and the new extensions spec — the
  installed axe version is the checker and now asserts itself.
- **axe 4.14 fallout fixed as code (no rule waivers — l07 forbids them)**:
  - `label-content-name-mismatch` (the 4.14-changed rule): command-palette
    trigger, vault switcher (name now composes from visible content + sr-only
    descriptor — Label-in-Name by construction), canvas save button (and its
    Saving transient), canvas version-rail buttons, the profile current-session
    revoke label, the DocumentCard delete button, and the admin-groups create
    button (redundant label dropped).
  - `color-contrast`: source chips in RightPane/SourceCards went solid
    (`bg-primary` + `text-primary-foreground`) — the `/10` tint compounded
    with the selected-card tint dropped `text-primary` below AA in light.
  - `aria-dialog-name`: the mobile More sheet's `aria-labelledby` now
    references the title's custom id explicitly.
- Full matrix at head with NO retries: 150 passed / 1 failed — the single
  failure is the [/wiki][320x568][light] color-contrast cell detailed under
  Caveats below (fails locally, passes on CI; disposition ASK).
- **New gate extensions (#871 AC6/AC7/AC8)** in
  `frontend/e2e/accessibility_gate_extensions.spec.ts`: a seeded
  post-render violation must be reported with exact rule + element + route
  within 30s; a synthetic tree under __dirname/test-results/ discriminates
  root selection (inside-root violation reported, outside-root NOT) via
  `axe.run(document.getElementById(...))` — the narrowly-allowlisted context
  shape; interactive surfaces (RightPane, Settings, DraftWorkspace, Canvas,
  mobile More) scanned with zero serious/critical and a falsifiable
  below-fold control check (pre-scroll geometry + exposure + viewport
  coverage).
- **l07 context carve (restricted in the feedback round)**:
  `scripts/check_l07_a11y_gate_wired.py` admits DOM-context arguments ONLY in
  `accessibility_gate_extensions.spec.ts` — every other gated spec (the
  frozen matrix included) must call `axe.run()` argument-less — with only
  empty calls, inline `document.getElementById|querySelector|
  querySelectorAll(<string literal>)`, and simple selector literals
  admitted there; every options/waiver/bypass shape (identifiers, arbitrary
  calls, parenthesized objects, second arguments) stays rejected, the
  matcher is linear-time (the pre-fix grammar admitted ~4.8s catastrophic
  backtracking — execution-proven, now pinned <1s), and the issue's original
  explicit-root clause is delivered: `--repo-root` selects the supplied tree
  under an in-repository temporary cwd. `backend/tests/test_l07_gate_wiring.py`
  pins all of it (22 tests).
- **CI budget (#871 AC8)**: the `Playwright a11y matrix` JOB's run step is
  wrapped in `timeout --kill-after=5s 492s` = 110% of the recorded 447s base
  baseline (job 139309699740); the two new specs run in their OWN
  `a11y-gate-extensions` CI job whose test step is bounded at 120s
  (install/build ride the job's 15-minute timeout).

## Why

The audit found no global focus guarantee, state-blind and non-identifying
accessible names, and an a11y gate whose checker identity (installed axe)
was never asserted — 4.14's `label-content-name-mismatch` behavior change
would have silently altered what CI measures.

## Migration

None. DevDependency bump only; no runtime API or schema changes.

## Caveats

- The dropped local C9 PRESERVING pin: the matrix is red on THIS Windows
  box for one cell ([/wiki][320x568][light] color-contrast on the wiki
  "Updated" timestamp) while CI passes that cell on ubuntu. Static
  computation puts the row at 3.89:1 vs the light background (below the
  4.5:1 that 12px text needs) — an arithmetic, platform-independent
  failure. The observed facts are: the cell fails locally and passes on CI,
  and the matrix keeps only definite violations (axe `incomplete` results
  are discarded), so a CI pass is not evidence the cell is clean; the exact
  reason axe does not flag it on CI is UNVERIFIED. Earlier drafts attributed
  the local/CI difference to a rendering-engine split — that attribution was
  wrong and is withdrawn. Disposition: ASK (maintainer) — see the PR body's
  Considered table.
- sr-only-input proxies (RadioGroupItem) keep their component ring as the
  visible indicator; the C4 spec measures the visible proxy — inside the
  New-draft dialog on /draft-room, behind HARD presence assertions
  (a missing control FAILS the leg; feedback-round F-001 fix), with the
  theme seeded via the persisted kv-theme key so dark/high-contrast legs
  measure their actual themes (F-002 fix) and the ring read from the
  WIDEST box-shadow layer, not ring-offset's opaque first layer (F-007 fix).
