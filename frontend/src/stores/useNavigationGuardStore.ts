import { create } from "zustand";

// ============================================================================
// A tiny, generic "confirm before leaving" registry.
//
// React Router's `useBlocker`/`unstable_usePrompt` require a data router
// (`createBrowserRouter` + `RouterProvider`); this app renders a plain
// `<BrowserRouter>` (see `App.tsx`), so those primitives throw at runtime
// and can't be used here. This store is the fallback boundary: a page with
// unsaved state registers a `confirmLeave` predicate while it's dirty, and
// any navigation entry point the DOM-click guard can't see — most notably
// button-triggered programmatic navigation, e.g. the mobile bottom nav's
// tab switcher — can consult it before calling `navigate()`.
// ============================================================================

interface NavigationGuardState {
  /** Returns true if navigation should proceed (not dirty, or user confirmed). */
  confirmLeave: (() => boolean) | null;
  setConfirmLeave: (fn: (() => boolean) | null) => void;
}

export const useNavigationGuardStore = create<NavigationGuardState>((set) => ({
  confirmLeave: null,
  setConfirmLeave: (fn) => set({ confirmLeave: fn }),
}));
