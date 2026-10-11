# 779: The shell stops misreporting where you are, contains a render error, and seeds its test-mode demo session before guards render

Issue: #779 ([Workstream L] PR 8 of 9; audit findings UI-R1-08, UI-R3-06,
UI-R2-06, UI-R3-08, class C17).

## What changed

- **Unmapped routes no longer mark Documents active**: `getActiveItemFromPath`
  in `App.tsx` now returns `NavItemId | null` with a `null` fall-through, and
  the `activeItem` prop is nullable through `NavigationProps` → `PageShell` →
  `MobileBottomNav`. On `/search` (and any future route no nav item owns), the
  mobile bottom nav marks nothing `aria-current="page"`, matching the desktop
  rail's own null-returning mapper, which is untouched.
- **Settings status badges wrap**: the `ConnectionStatusBadges` row gains
  `flex-wrap justify-end`, so at 320px the Backend/Embeddings/Chat badges wrap
  instead of overflowing off the left edge (clipping the first badge's icon)
  under the header's right-anchored stacking; the wrap stays right-anchored so
  it cannot introduce a rightward overflow.
- **Route-scoped error containment**: `PageShell` wraps its routed content in
  the existing `ErrorBoundary` (default "Something went wrong" / Try Again
  fallback, scoped to the page area), and the boundary component gained a
  `resetOnChange` prop: when the value changes (PageShell passes
  `location.key`), a caught error resets in that same render pass — the
  `resetKeys` pattern from react-error-boundary. A render error in any routed
  page now leaves navigation mounted and usable, and ANY navigation away —
  including a query-only re-search (`/search?q=a` → `/search?q=b`, which does
  not change the pathname) — renders the new content instead of holding the
  fallback. The existing top-level boundary in `App.tsx` remains as the last
  resort. Design note: the reset is a state reset, NOT a keyed remount — a
  key change at crash time re-throws inside React's error-recovery commit and
  escapes to the outer boundary, and remounting on every navigation would
  break same-component transitions like `/chat` -> `/chat/:id`, whose
  AnimatePresence exit window is load-bearing for the chat send and
  stop-stream flows (the #821 regression class; an earlier iteration of this
  fix did exactly that and broke the chat e2e). Page-transition animations
  are fully preserved for non-crash navigation. Known hazard (no current
  trigger): a page that throws during the exit window of a navigation AWAY
  from it shows the fallback at the destination URL; Try Again recovers.
- **Test-mode demo session seeds before guards render**: the demo-session
  seed moved from `ProtectedRoute`'s `useEffect` (which ran one commit AFTER
  `RoleGuard`'s synchronous `isAuthenticated` read, bouncing every `/admin/*`
  visit to the login form) into `App`, in two arms: a synchronous first-
  render seed — before the router subtree mounts, while no component is
  subscribed to the store yet — and an effect-based re-seed arm that restores
  the demo session after a later logout (a render-phase write on a mounted
  tree would trip React's "cannot update a component while rendering"
  warning). One seed — including `needsSetup: false` — is visible to
  `RoleGuard`, `LoginPage` and `SetupPage` alike, fixing the admin-route
  bounce and the eternal "Loading Meridian…" / "Verifying your instance"
  spinners on `/login` and `/setup`. The store write is mock-safe (optional
  call) so suites stubbing `useAuthStore` as a bare selector are unaffected.
  Production bundles get strictly smaller: the base bundle still shipped the
  old `ProtectedRoute` seed; this move is dead-code-eliminated behind the
  `import.meta.env.DEV` TEST_MODE gate.
- **Recurrence guardrail**: `frontend/src/shell-recurrence.l08.test.tsx`
  pins the class structurally — the synchronous first-render seed block must
  exist in `App`'s render body and precede every effect registration; raw
  `useAuthStore.setState` calls may not appear inside any effect body in
  `App.tsx` or `ProtectedRoute.tsx` (comment-stripped, brace-matched
  effect-body scan); `ProtectedRoute` stays seed-free; and the mobile nav
  must mark nothing `aria-current` for a neutral (null) active item.

## Test-mode visibility notes (DEV + `VITE_TEST_MODE=true` only)

The demo session signs the fixture user in for the whole dev session, so:
`/login` renders its form over the already-authenticated store (submitting
non-demo credentials reports "Invalid demo credentials"), and `/setup` and
`/register` redirect away (`/setup` to `/login` via the completed-setup
guard, `/register` via its own authenticated early-return) — the setup
wizard and registration form are unreachable in demo mode. All three are
fixture-mode semantics, not production behavior.

## Tests

- Frozen acceptance checks C1–C9 (authored at arm's length pre-fix,
  checkpoint-manifested and anchor-published on the issue): C1–C6 RED at base,
  GREEN after; C7–C9 (production auth redirect, RoleGuard fallback, More-sheet
  logout) GREEN throughout.
- Behavioral files carrying C1–C6: `App.l08-active-nav.test.tsx` (C1),
  `ConnectionStatusBadges.l08.test.tsx` (C2), `App.l08-error-boundary.test.tsx`
  (C3/C4), `App.l08-test-mode.test.tsx` (C5/C6).
- New recurrence guardrail `frontend/src/shell-recurrence.l08.test.tsx` (see
  What changed) plus `components/ErrorBoundary.test.tsx` — unit coverage for
  all three `resetOnChange` branches including the two-cycle
  crash→recover→crash→recover-back sequence — and `App.l08-feedback.test.tsx` /
  `App.l08-search-reset.test.tsx` — positive pins from the review round
  (the admin page itself renders in test mode; `/search` passes an empty
  active item; a query-only navigation recovers a crashed `/search`).

## Migration

None for API, store-persistence, or production-auth behavior. Visible
deltas: the badge row wraps at very narrow widths; the mobile nav no longer
highlights Documents on unmapped routes such as `/search`; a crashed page
recovers on any navigation (previously the whole shell was replaced); and
the dev-only test-mode notes above (`/login` form over the fixture session,
`/setup` and `/register` redirect away).
