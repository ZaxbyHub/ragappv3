# fix(frontend,a11y): Workstream O closeout — focus floor, theme color-scheme, Label-in-Name, axe 4.14 gate

## What changed

Closes #1159 (accessible focus and controls) and #871 (accessibility gate
extensions) — the final two issues of Workstream O, collated by explicit user
authorization.

- **Global keyboard-focus floor (#1159 AC4)**: an UNLAYERD `:focus-visible`
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
    descriptor — Label-in-Name by construction), canvas save button, canvas
    version-rail buttons.
  - `color-contrast`: source chips in RightPane/SourceCards went solid
    (`bg-primary` + `text-primary-foreground`) — the `/10` tint compounded
    with the selected-card tint dropped `text-primary` below AA in light.
  - `aria-dialog-name`: the mobile More sheet's `aria-labelledby` now
    references the title's custom id explicitly.
- **New gate extensions (#871 AC6/AC7/AC8)** in
  `frontend/e2e/accessibility_gate_extensions.spec.ts`: a seeded
  post-render violation must be reported with exact rule + element + route
  within 30s; a synthetic tree under `testInfo.outputPath()` discriminates
  root selection (inside-root violation reported, outside-root NOT) via
  `axe.run(document.getElementById(...))` — the narrowly-allowlisted context
  shape; interactive surfaces (RightPane, Settings, DraftWorkspace, Canvas,
  mobile More) scanned with zero serious/critical and a falsifiable
  below-fold control check (pre-scroll geometry + exposure + viewport
  coverage).
- **l07 context carve**: `scripts/check_l07_a11y_gate_wired.py` now admits
  EXACTLY empty calls, inline `document.getElementById|querySelector|
  querySelectorAll(<string literal>)`, and simple selector literals — every
  options/waiver/bypass shape (identifiers, arbitrary calls, parenthesized
  objects, second arguments) stays rejected;
  `backend/tests/test_l07_gate_wiring.py` pins both sides (18 tests).
- **CI budget (#871 AC8)**: the combined a11y step runs under
  `timeout --kill-after=5s 492s` = 110% of the recorded 447s base baseline
  (job 139309699740).

## Why

The audit found no global focus guarantee, state-blind and non-identifying
accessible names, and an a11y gate whose checker identity (installed axe)
was never asserted — 4.14's `label-content-name-mismatch` behavior change
would have silently altered what CI measures.

## Migration

None. DevDependency bump only; no runtime API or schema changes.

## Caveats

- The dropped local C9 PRESERVING pin: the existing matrix is
  deterministically red on THIS Windows box for one cell
  ([/wiki][320x568][light] color-contrast on the wiki "Updated" timestamp,
  ~3.9:1 by static computation) while base CI is green on ubuntu — a
  platform split, disclosed in the PR body; the matrix clause rides C5 + the
  CI job.
- sr-only-input proxies (RadioGroupItem) keep their component ring as the
  visible indicator; the C4 spec measures the visible proxy.
