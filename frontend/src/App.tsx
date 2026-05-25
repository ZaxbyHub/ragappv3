import { BrowserRouter, Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { PageShell } from "@/components/layout/PageShell";
import { useHealthCheck } from "@/hooks/useHealthCheck";
import { useEffect, lazy, Suspense } from "react";
import { useAuthStore } from "@/stores/useAuthStore";
import type { NavItemId } from "@/components/layout/navigationTypes";
import { Loader2 } from "lucide-react";
import { APP_BASENAME } from "@/lib/paths";

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
const NotFoundPage = lazy(() => import("@/pages/NotFoundPage"));
const WikiPage = lazy(() => import("@/pages/WikiPage"));
const KMSPage = lazy(() => import("@/pages/KMSPage"));
const KMSDetailPage = lazy(() => import("@/pages/KMSDetailPage"));

function PageLoader() {
  return (
    <div className="flex h-screen w-full items-center justify-center">
      <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
    </div>
  );
}

// Main app shell wrapper that provides the navigation and page layout
function MainAppShell({ children }: { children: React.ReactNode }) {
  const health = useHealthCheck({ pollInterval: 30000 });
  const navigate = useNavigate();
  const location = useLocation();

  // Determine active nav item from current route
  const getActiveItemFromPath = (pathname: string): NavItemId => {
    if (pathname.startsWith("/chat")) return "chat";
    if (pathname.startsWith("/documents")) return "documents";
    if (pathname.startsWith("/memory")) return "memory";
    if (pathname.startsWith("/wiki")) return "wiki";
    if (pathname.startsWith("/kms")) return "kms";
    if (pathname.startsWith("/vaults")) return "vaults";
    if (pathname.startsWith("/settings")) return "settings";
    if (pathname.startsWith("/admin/groups")) return "groups";
    if (pathname.startsWith("/admin/users")) return "users";
    if (pathname.startsWith("/admin/organizations")) return "organizations";
    if (pathname.startsWith("/profile")) return "profile";
    return "documents";
  };

  const activeItem = getActiveItemFromPath(location.pathname);

  const handleItemSelect = (id: string) => {
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
    <PageShell
      activeItem={activeItem}
      onItemSelect={handleItemSelect}
      healthStatus={health}
    >
      {children}
    </PageShell>
  );
}

function App() {
  const initAuth = useAuthStore((state) => state.init);

  useEffect(() => {
    initAuth();
  }, [initAuth]);

  return (
    <ErrorBoundary>
      <BrowserRouter
        basename={APP_BASENAME || undefined}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Suspense fallback={<PageLoader />}>
          <Routes>
              <Route path="/setup" element={<SetupPage />} />
              <Route path="/register" element={<RegisterPage />} />
              <Route path="/login" element={<LoginPage />} />
              <Route
                path="/chat"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <ChatShell />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              {/* /chat/redesign removed — redirect to canonical /chat */}
              <Route path="/chat/redesign" element={<ProtectedRoute><Navigate to="/chat" replace /></ProtectedRoute>} />
              <Route
                path="/chat/:sessionId"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <ChatShell />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />

              {/* Main app pages with shell */}
              <Route
                path="/documents"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <DocumentsPage />
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
                  <ProtectedRoute>
                    <MainAppShell>
                      <MemoryPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/vaults"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <VaultsPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/settings"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <SettingsPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />

              {/* Admin pages */}
              <Route
                path="/admin/users"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <AdminUsersPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/admin/groups"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <AdminGroupsPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/admin/organizations"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <OrgsPage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />

              <Route
                path="/profile"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
                      <ProfilePage />
                    </MainAppShell>
                  </ProtectedRoute>
                }
              />
              <Route
                path="/wiki"
                element={
                  <ProtectedRoute>
                    <MainAppShell>
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
