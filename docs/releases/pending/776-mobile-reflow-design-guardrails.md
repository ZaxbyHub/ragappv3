# 776: Mobile and header reflow, plus design-system guardrails part 2

Issue: #776 ([Workstream L] PR 5 of 9; frontier audit run 20260923T174456Z
findings UI-R1-01, UI-R4-05, UI-R4-03, UI-R2-02, UI-R4-06, UI-R3-02,
UI-R3-07, UI-R4-04, UI-R4-13 and proposal UI-ENH-12's PageHeader /
raw-palette / repeated-defect-dedup remainder; the proposal's
token-contrast-test metric is #777's).

## What changed

- **Chat clears the mobile bottom nav below `md`.** `PageShell`'s chat
  wrapper gains `pb-20 md:pb-0` (the fixed bottom nav measures ~73px plus the
  safe-area inset; 80px matches the non-chat branch's own calibration), so
  the composer toolbar no longer sits underneath it on phones. Desktop chat
  keeps its exact edge-to-edge layout (`md:pb-0`; the nav is `md:hidden`).
- **Canvas is no longer full-bleed.** `isChat` was an unanchored
  `/chat` prefix match, so `/chat/:id/canvas/:uid` inherited the chat
  wrapper's `overflow-hidden` no-padding layout. The match is now anchored to
  at most one path segment: `/chat`, `/chat/redesign` and `/chat/:sessionId`
  stay full-bleed; canvas gets the standard scrolling page wrapper.
- **Non-chat content has a measure.** The non-chat wrapper caps at
  `max-w-[1536px]`, so the long-inert `mx-auto` finally centers wide screens
  and Settings/Profile rows stop stretching across 4K monitors.
- **The mobile "More" sheet scrolls and has one close button.** Admin tile
  lists exceed 50vh, so the tile grid scrolls inside the sheet while the
  built-in close control stays pinned (the scroll lives on an inner region,
  so the absolutely positioned close never scrolls away). The hand-rolled
  close button is gone — every sheet already renders `SheetContent`'s
  built-in close control, so no sheet stacks two; the mobile chat evidence
  drawer does the same and drops its duplicate close, and the vendored
  close's touch target grows to ~40px (repo convention targets >=44px).
- **Every page header wraps, in one place.** `PageTitleHeader` is now the one
  page-header component (17 pages plus the canvas route): new
  `actions`/`before` slots render in `flex flex-wrap` rows, so the
  Wiki/KMS/Documents/detail header actions wrap instead of overflowing at
  320-768px, and the wrap rule lives in the shared component instead of per
  page. Six pages' hand-rolled `<h1>` headers (and canvas's) migrated onto
  it; KMS's bespoke bordered header bar and the detail pages' private header
  rows are consolidated. Disclosed behavior notes: KMS-detail's heading now
  wraps its edit-mode `<Input>` (the heading landmark survives editing);
  every migrated page adopts the shared title-chip look, so detail-page and
  canvas headings grow from `text-lg`/`text-2xl` to the shared `text-3xl`
  and long unbroken titles wrap mid-token instead of overflowing; the
  Draft Room detail "Back" link now sits beside the title rather than above
  it; long document filenames truncate again (block-level ellipsis with a
  shrinkable title chip).
- **The nav rail's scrollbar is discoverable.** `NavigationRail`'s nav
  `ScrollArea` passes `type="auto"` instead of Radix's default
  hover-only scrollbar, so at short viewports (720-768px) the Account
  section's existence is visible instead of hover-gated.
- **One password-rule statement.** New shared
  `PasswordRequirements` states exactly the three rules
  `password_strength_check` enforces (≥8 chars, one digit, one uppercase) —
  static sentence on Setup, Change-password, Create-user, Reset-password and
  Profile (which previously omitted the digit/uppercase rules or had nothing
  at all), and the live ✓/○ checklist on Register. The wording is
  contract-pinned to `auth_service.py` by
  `backend/tests/test_l05_password_rule_contract.py`.
- **Raw Tailwind palette classes are banned outside the design system.**
  194 occurrences across 27 non-test files now use the semantic tokens
  (amber/yellow→`warning`, emerald/green→`success`, red→`destructive`,
  indigo/violet/sky/blue→`primary`, matching the "Restrained Teal"
  monochrome theme; citation cards keep their type badges for distinction).
  Two guardrails share one allowlist (`frontend/raw-palette-allowlist.json`,
  empty): an inline ESLint rule `local/no-raw-palette` in
  `frontend/eslint.config.js` (off for `components/ui`, tests, and allowlisted
  files) and `scripts/check_l05_raw_palette.py` (CI Quality contracts; budget
  ≤10). Visual notes: token hues differ from the raw palette they replace —
  on light theme the primary-on-tint chips (citation chips, wiki label pills,
  thinking/instant badges at 10px) now sit below the 4.5:1 AA text floor
  (~3.1-4.2:1) where the previous violet/indigo-700 pairings passed; #777
  exists precisely to bring token contrast to AA and pins it with a test.
  The thinking and instant mode badges now share one accent (icon + label
  carry the distinction); chat citation cards shift from indigo/emerald/amber
  to the primary/success/warning tokens, and search-highlight marks use
  lighter tints than before.
- **One page-header ownership guardrail.**
  `scripts/check_l05_page_headers.py` fails CI when any non-test page under
  `frontend/src/pages` renders its own literal `<h1>` instead of the shared
  component. Both new scripts are wired into `ci.yml`, the `justfile`
  `quality-contracts` recipe (comment corrected to "all twelve"), both
  engineering doc inventories, and `AGENTS.md`.

## Verification

- Frozen acceptance checks C1-C13 (issue #776 red-checkpoint anchor,
  manifest e760ce79…): C1-C11 RED→GREEN, C12-C13 GREEN→GREEN.
- `npm run typecheck`, `npm run lint -- --max-warnings 0` (includes the new
  rule), full `npm test`, `npm run build`; backend `ruff check .` and
  `pytest`; all `scripts/check_*.py` including the two new census scripts;
  Playwright e2e smoke (incl. the 390px chat-width-budget spec).
