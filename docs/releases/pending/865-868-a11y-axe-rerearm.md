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
  that re-filter one list are not tabs per the ARIA Practices). For real tab
  sets, Radix keeps an empty panel shell mounted but unmounts the panel's
  CONTENT on inactive tabs; the shared `TabsContent` wrapper now `forceMount`s
  panels with `data-[state=inactive]:hidden`, keeping every panel's content in
  the DOM (Settings, Canvas, RightPane, DraftWorkspace, DraftInspector) —
  every trigger's `aria-controls` resolves to a mounted, populated panel.
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
  renders a labelled list parent (the no-results state stays roleless).

## Gate and CI changes

- `frontend/e2e/a11y-matrix.spec.ts` runs axe with the full default rule set
  (the amendment-5 waiver rows are removed).
- New focused probe suite `frontend/e2e/a11y-rearm-865-868.spec.ts` runs in
  the same CI job: aria-controls resolution, no interactive-inside-interactive
  on /wiki cards, native list semantics on /chat.
- `scripts/check_l07_a11y_gate_wired.py` now enforces **zero** disabled rules
  (whitespace-tolerant scan, comments ignored) instead of the exactly-four
  waiver roster; its pytest mutants pin any-waiver, spacing-variant, and
  comment-tolerance behavior (`backend/tests/test_l07_gate_wiring.py`).
- New source-census guardrail `frontend/src/a11y.unnamed-controls-census.test.ts`
  fails on any new unnamed icon-only button, placeholder-only Select trigger
  (absent an associated label or pinned value), or FolderTree-style empty
  unlabeled plain button.

## Known limitations

- `TabsContent forceMount` mounts every panel eagerly: tab-panel effects and
  queries (e.g. Canvas compare's version queries, Maintenance jobs fetch) now
  run at page load instead of on first activation. Hidden panels are
  `display: none`, so they stay out of the tab order, accessibility tree, and
  the gate's offender probe.
- The keyboard model of the two converted filter bars changes from Radix
  roving-arrow focus to plain buttons (each filter is its own tab stop).
- The upload indicator's whole-header click no longer toggles the panel; the
  labeled collapse/expand button (which also carries `aria-expanded`) owns
  the toggle.
