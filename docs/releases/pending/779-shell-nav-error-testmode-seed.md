# 779: The shell stops misreporting where you are, contains a render error, and controls its test-mode demo session

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
- **Route-scoped error containment**: `PageShell` keeps one persistent
  `ErrorBoundary` around routed content and passes a boundary token made from
  `location.pathname`, `location.search`, and `location.hash`. A page render
  error stays inside the page area, so navigation remains mounted and a route
  change can recover the content. The existing pathname motion key stays in
  place for page transitions; the tuple token also lets query/hash state
  changes at a constant pathname reset the caught page state without changing
  the navigation shell. The real chat smoke covers the send and stop-stream
  flows. A page that throws while it is exiting during navigation away can
  still leave the fallback visible at the destination URL; Try Again recovers
  that page. This correction does not resolve the exit-window hazard. A
  fragment-only change on a crashed route can also reset and
  re-render the child once, which is bounded and part of the hash-recovery
  behavior.
- **Test-mode demo session bootstrap**: `App` now uses a one-time router-local
  layout-effect gate for DEV-only `TEST_MODE` bootstrap. The gate skips
  `/register` and preserves logout, then seeds one demo session (including
  `needsSetup: false`) before guarded admin/session routes settle. This avoids
  the admin-route bounce and the eternal "Loading Meridian…" /
  "Verifying your instance" spinners on `/login` and `/setup`.
  The existing base already gates this demo-only seed behind
  `import.meta.env.DEV`; this corrective change moves its lifecycle ownership
  into the router-local layout effect while keeping the seed development-only.
  The initial `/register` form stays public; after a session exists, registration
  retains its normal authenticated redirect.
  In DEV-only `TEST_MODE`, the seeded `needsSetup: false` makes `/setup`
  redirect to `/login`, where the demo-credential form is rendered; the setup
  wizard remains the production setup flow.

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
- Recurrence coverage combines minimal structural checks for the DEV-only
  `TEST_MODE` condition and absence of guard-owned store writes with a
  null-activeItem mobile-nav component contract. Real App tests cover
  bootstrap ordering, the `/register` transition, and logout preservation.
- The real chat smoke validates the send and stop-stream flows, while the
  route-scoped boundary coverage preserves query/hash state at a constant
  pathname.

## Migration

No API, store-persistence, or production-auth-behavior contract change. The frontend
migration includes the DEV-only test bootstrap and explicit mobile /search active-nav handling:
the mobile nav leaves every control unselected on /search, while /documents and /chat map to their respective controls. The other visible production deltas are badge wrapping at very narrow widths and the crash-containment/navigation behavior above.
