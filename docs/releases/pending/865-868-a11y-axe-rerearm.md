# a11y gate re-arm: the four waived axe rule families are enforced again (#865, #866, #867, #868)

The #778 route×viewport a11y gate shipped with four axe rule families disabled
(user-sanctioned amendment 5, 2026-10-09) pending four follow-up issues. This
change fixes every underlying violation and re-enables the full default rule
set, so the matrix (23 routes × 320×568/640×360 × light/dark) now enforces:

- **#865 — `aria-valid-attr-value` (critical)**: Radix emits `aria-controls`
  on every tab trigger unconditionally. The two measured violators were
  trigger-only **filter bars** (Wiki page-type filter, Draft Room status
  filter) that rendered no panels at all; they are now `role="group"`
  toggle-button groups (`aria-pressed`, each filter its own tab stop — filters
  that re-filter one list are not tabs per the ARIA Practices). Real tab sets
  needed no component change: Radix keeps every inactive panel in the DOM as a
  hidden shell, so each trigger's `aria-controls` already resolved to a
  mounted element once the filter bars stopped emitting dangling ids. The
  shared `TabsContent` wrapper is unchanged, and a new jsdom pin
  (`ui/tabs.test.tsx`) locks that shell-resolution contract in.
- **#866 — `nested-interactive` (serious)**: clickable-card containers that
  nested interactive controls are gone. Wiki page cards: the open action
  moved to the title button, the per-row checkbox keeps its own control.
  Chat source cards: the filename button carries the open action, the
  More/Less expander is a sibling. The upload indicator header is a layout
  row with a labeled collapse button; the clear action sits beside it.
  Session-rail rows are `listitem`s whose title button carries activation
  and the roving tabindex (the UI-045 wrapper-focus algorithm is unchanged).
- **#867 — `button-name` (critical)**: the Documents folder-tree leaf
  chevron is now a non-interactive spacer (it previously rendered an empty,
  unlabeled button); the Memories search, wiki-page delete, and wiki-jobs
  refresh icon buttons carry aria-labels. Sites already named at runtime via
  associated `<label htmlFor>` triggers or visible values are deliberately
  untouched (a blanket aria-label there would break WCAG 2.5.3
  Label-in-Name) — the amended census test documents every exemption class.
- **#868 — `aria-required-children` (critical)**: the chat suggested-prompts
  grid is a native `<ul>`/`<li>`; the RightPane Sources lists (virtualized
  and plain) and the evidence-candidates block declare `role="list"` with
  `listitem` item wrappers; every SessionRail row path that renders listitems
  renders a labelled list parent (the no-results state stays roleless, and
  the inner list is labelled "Session list" so it does not duplicate the
  surrounding landmark's name).

## Gate and CI changes

- `frontend/e2e/a11y-matrix.spec.ts` runs axe with the full default rule set
  (the amendment-5 waiver rows are removed).
- `scripts/check_l07_a11y_gate_wired.py` enforces that invariant structurally:
  **every `axe.run(` call in every `frontend/e2e/*.spec.ts` must be
  argument-less**. Any option object — `rules`/`enabled: false` under any
  quoting or line split, `runOnly`, `exclude` — fails CI, as do job-neutering
  spellings (`if:` conditions evaluating false by any common spelling;
  `continue-on-error: "true"/True`). Its pytest mutants pin each shape
  (`backend/tests/test_l07_gate_wiring.py`).
- New focused probe suite `frontend/e2e/a11y-rearm-865-868.spec.ts` runs in
  the same CI job: the /wiki card no-nesting contract and the /chat native
  list contract, plus the historical dangling-aria-controls probe over
  /wiki and /draft-room (the routes whose filter bars produced the base
  violations — those routes now render no tabs, and the axe matrix itself
  enforces `aria-valid-attr-value` on all 23 routes every run).
- New source-census guardrail `frontend/src/a11y.unnamed-controls-census.test.ts`
  fails on any new unnamed icon-only shadcn `<Button>` (inline `<svg>` counts
  as an icon; JSX comments do not count as a name; `aria-label={undefined}`,
  `aria-hidden="false"`, and empty-`title` exemptions require live values),
  any genuinely unnamed placeholder-only Select trigger (absent an
  associated label or a pinned value), and the FolderTree-style empty
  unlabeled plain button.

## Known limitations

- The keyboard model of the two converted filter bars changes from Radix
  roving-arrow focus to plain buttons (each filter is its own tab stop).
- Three interactive containers lost their whole-surface click targets, with
  the action preserved on a named inner control: wiki page cards (title
  button), chat source cards (filename button), and session-rail rows (title
  button; the row's cursor-pointer affordance went with the role). The
  upload indicator's whole-header click similarly moved to its labeled
  collapse button, and filename hover tooltips (title attr) were dropped
  where the filename became the button.
- The census is a source-scanning guard, not a compiler: member-expression
  tags (`<UI.Button>`), props-spread wrappers, cross-component label
  association, spread-supplied placeholders, and value-pinned Selects
  without a matching item are documented exemption classes, and its native
  `<button>` leg targets the conditional-`aria-label` FolderTree shape
  specifically.
