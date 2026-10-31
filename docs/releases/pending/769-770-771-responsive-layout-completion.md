# Responsive layout completion — sticky table Actions, md-band cards, result grids, 80ch prose, admin/vault card fallbacks, evidence-sheet snap

Issues: #769, #770, #771 (Workstream K PRs 6-8 of 8, collated)
PR: (this PR)

## Documents and detail layouts (#769)

- `DocumentTable`'s Actions column is now `sticky right-0` with an opaque
  background and a separator border, so row actions stay visible at laptop
  widths without horizontal scrolling; a Delete hit-test pins clickability.
- The Documents card/table switch moved from `sm` to `md`, closing the
  640-767px band that previously rendered the scrolling table.

## Content width (#770)

- `/search` and `/memory` results render as multi-column grids on wide
  viewports (`sm:grid-cols-2 lg:grid-cols-3`, the KMSPage pattern); the
  result-count line stays outside the grid.
- Memory prose paragraphs carry `max-w-[80ch]`, bounding line length by
  characters at any column width.

## Admin and vault tables (#771)

- The five `Table` consumers (AdminUsersPage, GroupTable, OrgsPage members,
  VaultMembersPanel, VaultGroupAccessPanel) render below-`sm` card lists via
  shared render helpers — bit-for-bit the same controls, handlers, permission
  conditions and pending states — with card-visible loading/error/empty
  states. Colliding test queries are table-scoped.

## Evidence sheet snap (#771)

- The below-lg evidence sheet gains a peek (45vh) ↔ full (85vh) snap point
  with a `touch-action: none` handle that is both an accessible toggle and a
  drag handle (snap commits only on completed pointerup; cancels abort
  without snapping; post-drag click suppressed and cleared at gesture start).
  The chat column padding tracks the expanded state; expansion resets on
  every close/breakpoint path.

## Tests / CI

- Four new Playwright specs (`k06/k07/k08-*`) join the e2e job's explicit
  list; two k08 vitest stand-ins cover the unrouted vault panels; three
  compact-class pins guard the CSS-switch class side; the a11y matrix's
  admin sentinels are visible-scoped for the card lists.
