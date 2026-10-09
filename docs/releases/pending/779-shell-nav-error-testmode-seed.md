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
  `location.pathname`), a caught error resets in that same render pass — the
  `resetKeys` pattern from react-error-boundary. A render error in any routed
  page now leaves navigation mounted and usable, and navigating away renders
  the new route instead of holding the fallback. The existing top-level
  boundary in `App.tsx` remains as the last resort. Design note: the reset is
  a state reset, NOT a keyed remount — a key change at crash time re-throws
  inside React's error-recovery commit and escapes to the outer boundary,
  and keying by pathname on every navigation would remount the subtree for
  same-component transitions like `/chat` -> `/chat/:id`, whose
  AnimatePresence exit window is load-bearing for the chat send and
  stop-stream flows (the #821 regression class; an earlier iteration of this
  fix did exactly that and broke the chat e2e). Page-transition animations
  are fully preserved for non-crash navigation.
- **Test-mode demo session seeds synchronously**: the demo-session seed moved
  from `ProtectedRoute`'s `useEffect` (which ran one commit AFTER
  `RoleGuard`'s synchronous `isAuthenticated` read, bouncing every `/admin/*`
  visit to the login form) into `App`'s render path, before the router
  subtree mounts. One seed — including `needsSetup: false` — is now visible
  to `RoleGuard`, `LoginPage` and `SetupPage` alike, fixing all three
  symptoms: the admin-route bounce, and the eternal "Loading Meridian…" /
  "Verifying your instance" spinners on `/login` and `/setup`. The store
  write is mock-safe (optional call) so suites stubbing `useAuthStore` as a
  bare selector are unaffected, and production builds are untouched: the
  seed is dead-code-eliminated behind the `import.meta.env.DEV` TEST_MODE
  gate, as before.

## Why

Audit 20260923T174456Z findings (all re-verified at merge base 16925def): the
mobile nav misreported location on unmapped routes; a capture-invisible
leftward overflow; one error boundary wider than any page's blast radius; and
a dev-fixture seed racing the guards it was meant to satisfy (all 48 `/admin/*`
capture runs showed the login form; `/login` and `/setup` spun forever).

## Tests

- Frozen acceptance checks C1–C9 (authored at arm's length pre-fix,
  checkpoint-manifested and anchor-published on the issue): C1–C6 RED at base,
  GREEN after; C7–C9 (production auth redirect, RoleGuard fallback, More-sheet
  logout) GREEN throughout.
- New recurrence guardrail `frontend/src/shell-recurrence.l08.test.tsx`: a
  source census pinning the seed's synchronous placement (RED on base) and a
  null-activeItem component contract for the mobile nav.

## Migration

None. No API, store-persistence, or production-auth-behavior change; the only
visible production deltas are the badge row wrapping at very narrow widths
and the crash-containment/navigation behavior above.
