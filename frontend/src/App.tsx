import { BrowserRouter, Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import { TestModeProvider } from "@/fixtures/TestModeContext";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { PageShell } from "@/components/layout/PageShell";
import { useHealthCheck } from "@/hooks/useHealthCheck";
import { useCallback, useEffect, useRef, lazy, Suspense } from "react";
import { useAuthStore } from "@/stores/useAuthStore";
import { useNavigationGuardStore } from "@/stores/useNavigationGuardStore";
import type { NavItemId } from "@/components/layout/navigationTypes";
import { Loader2 } from "lucide-react";
import { APP_BASENAME } from "@/lib/paths";
import ReconnectingBanner from "@/components/ReconnectingBanner";
import { CommandPalette } from "@/components/shared/CommandPalette";
import {
  KeyboardShortcutsDialog,
  useKeyboardShortcuts,
} from "@/components/shared/KeyboardShortcuts";
import { useCommandPaletteAction, type CommandPaletteActionGuard } from "@/lib/commandPaletteActions";
import { useThemeStore } from "@/stores/useThemeStore";
import { DocumentsTableSkeleton } from "@/components/documents/DocumentsTableSkeleton";

// Toggle mock-data mode via: VITE_TEST_MODE=true npm run dev
// Gated on import.meta.env.DEV so it is statically false (and dead-code
// eliminated) in any production build — the demo auth bypass and mock
// fixtures can never ship in `vite build` output regardless of VITE_TEST_MODE.
const TEST_MODE = import.meta.env.DEV && import.meta.env.VITE_TEST_MODE === "true";

// H-16 fix: Lazy-load all page components for code splitting
const ChatShell = lazy(() => import("@/pages/ChatShell"));
const DocumentsPage = lazy(() => import("@/pages/DocumentsPage"));
const DocumentDetailPage = lazy(() => import("@/pages/DocumentDetailPage"));
const MemoryPage = lazy(() => import("@/pages/MemoryPage"));
const VaultsPage = lazy(() => import("@/pages/VaultsPage"));
const SettingsPage = lazy(() => import("@/pages/SettingsPage"));
const LoginPage = lazy(() => import("@/pages/LoginPage"));
const SetupPage = lazy(() => import("@/pages/SetupPage"));
const RegisterPage = lazy(() => import("@/pages/RegisterPage"));
const AdminUsersPage = lazy(() => import("@/pages/AdminUsersPage"));
const AdminGroupsPage = lazy(() => import("@/pages/AdminGroupsPage"));
const OrgsPage = lazy(() => import("@/pages/OrgsPage"));
const ProfilePage = lazy(() => import("@/pages/ProfilePage"));
const ChangePasswordRequiredPage = lazy(() => import("@/pages/ChangePasswordRequiredPage"));
const NotFoundPage = lazy(() => import("@/pages/NotFoundPage"));
const WikiPage = lazy(() => import("@/pages/WikiPage"));
const KMSPage = lazy(() => import("@/pages/KMSPage"));
const KMSDetailPage = lazy(() => import("@/pages/KMSDetailPage"));
// Unified discovery surface (issue #515 / PRODUCT-ENH-11) — reached via the
// shell's global searchbox or a direct /search?q=… link.
const SearchPage = lazy(() => import("@/pages/SearchPage"));
const DraftRoomPage = lazy(() => import("@/pages/DraftRoomPage"));
const DraftRoomDetailPage = lazy(() => import("@/pages/DraftRoomDetailPage"));
const CanvasPage = lazy(() => import("@/components/canvas/CanvasPage"));

function PageLoader() {
  return (
    <div className="flex h-screen w-full items-center justify-center" role="status" aria-label="Loading page">
      <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" aria-hidden="true" />
    </div>
  );
}

// Documents-page-shaped Suspense fallback (issue #258 / legacy-14): routes
// that own a page-level skeleton use it instead of the generic PageLoader so
// the shell never flashes a full-screen spinner for a known page shape.
// Bounded to DocumentsPage (the one page with an existing table skeleton);
// every other route keeps the global PageLoader fallback.
function DocumentsPageFallback() {
  return (
    <div
      className="space-y-6 animate-in fade-in duration-300 pb-12"
      role="status"
      aria-label="Loading documents page"
    >
      <DocumentsTableSkeleton />
    </div>
  );
}

// App-wide keyboard-shortcuts surface (issue #775): the hook's listener and
// the dialog live at the shell level so both the "?" shortcut and the
// palette's "Show keyboard shortcuts" action (which dispatches the bound
// combo) reach them on every route. Chat-page behavior is unchanged — the
// same listener/dialog ChatShell used to mount, one level up.
function AppShortcutsMount() {
  const { open: shortcutsOpen, setOpen: setShortcutsOpen } = useKeyboardShortcuts();
  const { pathname } = useLocation();
  const shortcutsActionGuardRef = useRef<CommandPaletteActionGuard | null>(null);
  const shortcutsOpeningTokenRef = useRef<symbol | null>(null);
  const shortcutsOpeningGuardRef = useRef<CommandPaletteActionGuard | null>(null);
  const commandPaletteShortcutsGuard = useCommandPaletteAction({
    id: "open-keyboard-shortcuts",
    label: "Open keyboard shortcuts",
    enabled: pathname === "/chat",
    execute: (dispatchGuard) => {
      if (!dispatchGuard.isCurrent()) return;
      const consumerGuard = shortcutsActionGuardRef.current;
      if (!consumerGuard?.isCurrent()) return;
      shortcutsOpeningTokenRef.current = Symbol("keyboard-shortcuts");
      shortcutsOpeningGuardRef.current = consumerGuard;
      setShortcutsOpen(true);
    },
  });
  shortcutsActionGuardRef.current = commandPaletteShortcutsGuard;
  const renderedShortcutsOpeningToken = shortcutsOpeningTokenRef.current;
  const renderedShortcutsOpeningGuard = shortcutsOpeningGuardRef.current;
  const handleShortcutsOpenChange = useCallback((nextOpen: boolean) => {
    if (shortcutsOpeningTokenRef.current !== renderedShortcutsOpeningToken) return;
    if (renderedShortcutsOpeningGuard && !renderedShortcutsOpeningGuard.isCurrent()) {
      shortcutsOpeningTokenRef.current = null;
      shortcutsOpeningGuardRef.current = null;
      setShortcutsOpen(false);
      return;
    }
    if (nextOpen) {
      setShortcutsOpen(true);
      return;
    }
    shortcutsOpeningTokenRef.current = null;
    shortcutsOpeningGuardRef.current = null;
    setShortcutsOpen(false);
  }, [renderedShortcutsOpeningGuard, renderedShortcutsOpeningToken, setShortcutsOpen]);
  useEffect(() => {
    const openingGuard = shortcutsOpeningGuardRef.current;
    if (shortcutsOpen && shortcutsOpeningTokenRef.current && openingGuard && !openingGuard.isCurrent()) {
      shortcutsOpeningTokenRef.current = null;
      shortcutsOpeningGuardRef.current = null;
      setShortcutsOpen(false);
    }
  }, [commandPaletteShortcutsGuard, setShortcutsOpen, shortcutsOpen]);
  return (
    <KeyboardShortcutsDialog open={shortcutsOpen} onOpenChange={handleShortcutsOpenChange} />
  );
}

function CapabilityCommandPalette() {
  const { pathname } = useLocation();
  const { theme, setTheme } = useThemeStore();
  useCommandPaletteAction({
    id: "cycle-theme",
    label: "Cycle theme",
    enabled: pathname === "/chat",
    execute: (guard) => {
      if (!guard.isCurrent()) return;
      setTheme(theme === "light" ? "dark" : theme === "dark" ? "system" : "light");
    },
  });
  return <CommandPalette />;
}

// Main app shell wrapper that provides the navigation and page layout
function MainAppShell({ children, testMode = false }: { children: React.ReactNode; testMode?: boolean }) {
  const health = useHealthCheck({ pollInterval: 30000 });
  const navigate = useNavigate();
  const location = useLocation();

  // Determine active nav item from current route. null means no nav item
  // owns the route (e.g. /search, per its own route comment) — the mobile
  // bottom nav then marks nothing aria-current, matching the desktop rail's
  // own null-returning mapper (issue #779 / UI-R1-08).
  const getActiveItemFromPath = (pathname: string): NavItemId | null => {
    if (pathname.startsWith("/chat")) return "chat";
    if (pathname.startsWith("/documents")) return "documents";
    if (pathname.startsWith("/memory")) return "memory";
    if (pathname.startsWith("/wiki")) return "wiki";
    if (pathname.startsWith("/kms")) return "kms";
    if (pathname.startsWith("/draft-room")) return "draftRoom";
    if (pathname.startsWith("/vaults")) return "vaults";
    if (pathname.startsWith("/settings")) return "settings";
    if (pathname.startsWith("/admin/groups")) return "groups";
    if (pathname.startsWith("/admin/users")) return "users";
    if (pathname.startsWith("/admin/organizations")) return "organizations";
    if (pathname.startsWith("/profile")) return "profile";
    return null;
  };

  const activeItem = getActiveItemFromPath(location.pathname);

  const handleItemSelect = (id: string) => {
    // The mobile bottom nav switches tabs via a plain `<button onClick>`
    // (no `<a href>`), so Draft Room's unsaved-changes guard can't see it
    // through its click-based interceptor. Consult the same guard here —
    // see `useNavigationGuardStore` / `DraftRoomDetailPage`.
    const confirmLeave = useNavigationGuardStore.getState().confirmLeave;
    if (confirmLeave && !confirmLeave()) return;
    switch (id) {
      case "chat":
        navigate("/chat");
        break;
      case "chatNew":
        navigate("/chat");
        break;
      case "documents":
        navigate("/documents");
        break;
      case "memory":
        navigate("/memory");
        break;
      case "wiki":
        navigate("/wiki");
        break;
      case "kms":
        navigate("/kms");
        break;
      case "draftRoom":
        navigate("/draft-room");
        break;
      case "vaults":
        navigate("/vaults");
        break;
      case "settings":
        navigate("/settings");
        break;
      case "groups":
        navigate("/admin/groups");
        break;
      case "users":
        navigate("/admin/users");
        break;
      case "organizations":
        navigate("/admin/organizations");
        break;
      case "profile":
        navigate("/profile");
        break;
      default:
        navigate("/documents");
    }
  };

  return (
    <TestModeProvider testMode={testMode}>
      {/* Global command palette (issue #258 / legacy-14; v2 per issue #775):
          Ctrl/Cmd+K opens the palette from shell routes off /chat (the chat
          rail owns the combo on /chat by design). Closed state renders
          nothing, so it is layout-inert. The keyboard-shortcuts
          surface ("?" + the dialog) is mounted HERE at the app shell since
          issue #775 — one mount on every shell route instead of
          ChatShell-only — so the palette's "Show keyboard shortcuts" action
          and the "?" shortcut work app-wide through the single existing
          dispatcher. */}
      <CapabilityCommandPalette />
      <AppShortcutsMount />
      <PageShell
        activeItem={activeItem}
        onItemSelect={handleItemSelect}
        healthStatus={health}
      >
        {children}
      </PageShell>
    </TestModeProvider>
  );
}

// Demo user for the dev-only fixture mode (moved from ProtectedRoute with
// issue #779 / UI-R3-08 — the synchronous seed block lives in App's render,
// below).
function getDemoUser() {
  const role = import.meta.env.VITE_DEMO_ROLE || "superadmin";
  return {
    id: 1,
    username: import.meta.env.VITE_DEMO_USERNAME || "demo",
    full_name: import.meta.env.VITE_DEMO_FULL_NAME || "Demo User",
    role: role as "superadmin" | "admin" | "member" | "viewer",
    is_active: true,
  };
}

// Issue #779 (UI-R3-08): seed the demo session synchronously, BEFORE any
// guard or auth-reading page renders. The old seed lived in ProtectedRoute's
// useEffect — one commit too late for RoleGuard's synchronous
// isAuthenticated read (every /admin/* visit bounced to /login on the first
// render) and invisible to LoginPage/SetupPage, which skip init() in test
// mode and spun on needsSetup === null forever. Writing here, in App's
// render before the router subtree mounts, makes the same seed visible to
// all three consumers on the very first render. The write is mock-safe:
// suites that stub useAuthStore as a bare selector (no setState) no-op
// through the optional call; on the first render no other component is
// subscribed yet, and the isAuthenticated guard keeps it idempotent
// (re-seeding after logout matches the old effect's behavior).
function App() {
  const initAuth = useAuthStore((state) => state.init);
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const health = useHealthCheck({ pollInterval: 30000 });

  if (TEST_MODE && !isAuthenticated) {
    const setState = useAuthStore.setState as typeof useAuthStore.setState | undefined;
    setState?.({
      user: getDemoUser(),
      accessToken: "demo-token",
      isAuthenticated: true,
      isInitialized: true,
      needsSetup: false,
      isLoading: false,
      authMode: "jwt",
    });
  }

  useEffect(() => {
    if (!TEST_MODE) {
      initAuth();
    }
  }, [initAuth]);

  return (
    <ErrorBoundary>
      <ReconnectingBanner health={health} />
      <BrowserRouter
        basename={APP_BASENAME || undefined}
      >
        <Suspense fallback={<PageLoader />}>
          <Routes>
              <Route path="/setup" element={<SetupPage />} />
              <Route path="/register" element={<RegisterPage />} />
              <Route path="/login" element={<LoginPage />} />
              {/* Forced password change — shell-less; ProtectedRoute routes
                  flagged users here and blocks all other routes until cleared. */}
              <Route
                path="/change-password"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <ChangePasswordRequiredPage />
                  </ProtectedRoute>
                }
              />
              <Route
                path="/chat"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <ChatShell />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              {/* /chat/redesign removed — redirect to canonical /chat */}
              <Route path="/chat/redesign" element={<ProtectedRoute testMode={TEST_MODE}><Navigate to="/chat" replace /></ProtectedRoute>} />
              <Route
                path="/chat/:sessionId"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <ChatShell />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              {/* Versioned code/document canvas opened from a chat answer
                  (issue #509). Same PageShell/protected wrapper as the chat
                  routes; 404/disabled states render inside CanvasPage. */}
              <Route
                path="/chat/:sessionId/canvas/:artifactUid"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <CanvasPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />

              {/* Main app pages with shell */}
              <Route
                path="/documents"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      {/* Inner Suspense: the shell stays mounted while the
                          page chunk loads; fallback matches the page shape. */}
                      <Suspense fallback={<DocumentsPageFallback />}>
                        <DocumentsPage />
                      </Suspense>
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/documents/:documentId"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <DocumentDetailPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/memory"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <MemoryPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/vaults"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <VaultsPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/settings"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <SettingsPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />

              {/* Admin pages */}
              <Route
                path="/admin/users"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <AdminUsersPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/admin/groups"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <AdminGroupsPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/admin/organizations"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <OrgsPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />

              <Route
                path="/profile"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <ProfilePage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/wiki"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <WikiPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/kms"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <KMSPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/kms/:entryId"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <KMSDetailPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />

              {/* Unified discovery search results (issue #515 / PRODUCT-ENH-11).
                  The shell's global searchbox navigates here with ?q=…&types=…;
                  no nav item owns it, so the rail keeps no active highlight. */}
              <Route
                path="/search"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <SearchPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />

              {/* Registered unconditionally — a direct visit while the capability is
                  disabled must render the page's own honest disabled state, never a
                  silent redirect or a 404 (issue #437). Only the nav entry is gated. */}
              <Route
                path="/draft-room"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <DraftRoomPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/draft-room/:draftId"
                element={
                  <ProtectedRoute testMode={TEST_MODE}>
                    <MainAppShell testMode={TEST_MODE}>
                      <DraftRoomDetailPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />

              {/* Redirect root to documents */}
              <Route path="/" element={<Navigate to="/documents" replace />} />

              {/* H-12 fix: Proper 404 page instead of silently rendering DocumentsPage */}
              <Route path="/*" element={<NotFoundPage />} />
            </Routes>
          </Suspense>
      </BrowserRouter>
    </ErrorBoundary>
  );
}

export default App;
