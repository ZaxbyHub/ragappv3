import { useEffect, useRef } from "react";
import { captureAuthOwner, captureAuthPrincipalGeneration, isCurrentAuthOwner } from "@/lib/api/auth-lifecycle";
import { Navigate, useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/useAuthStore";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";

interface ProtectedRouteProps {
  children: React.ReactNode;
  testMode?: boolean;
}

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

export function ProtectedRoute({ children, testMode = false }: ProtectedRouteProps) {
  const location = useLocation();

  // H-10 fix: Use only the JWT auth store — legacy AuthContext OR removed
  const { isAuthenticated, isLoading, isInitialized, initializationFailed, needsSetup, user } = useAuthStore();
  const init = useAuthStore((state) => state.init);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // testMode is a development-only convenience (see App.tsx, where it is gated
  // by import.meta.env.DEV so it can never be enabled in a production build).
  // Seed a demo session once — in an effect, not during render — so protected
  // routes are reachable without a backend. Pages use useTestMode() for mock data.
  useEffect(() => {
    if (testMode && !isAuthenticated) {
      useAuthStore.setState({
        user: getDemoUser(),
        accessToken: "demo-token",
        isAuthenticated: true,
        isInitialized: true,
        needsSetup: false,
        isLoading: false,
        authMode: "jwt",
      });
    }
  }, [testMode, isAuthenticated]);

  if (testMode) {
    return <>{children}</>;
  }

  // Show a recoverable error when authentication setup initialization fails.
  if (initializationFailed) {
    const retryOwner = captureAuthOwner();
    const retryPrincipalGeneration = captureAuthPrincipalGeneration();
    const retry = () => {
      if (!mountedRef.current || !isCurrentAuthOwner(retryOwner)) return;
      if (captureAuthPrincipalGeneration() !== retryPrincipalGeneration) return;
      void init();
    };
    return (
      <div className="flex h-screen w-full flex-col items-center justify-center gap-4" role="alert">
        <p>Unable to initialize authentication.</p>
        <Button type="button" variant="outline" onClick={retry}>Retry</Button>
      </div>
    );
  }

  // Show loading while auth state or setup check is still initializing
  // RT-02 fix: wait for init() to complete before making auth decisions
  if (isLoading || !isInitialized || needsSetup === null) {
    return (
      <div className="flex h-screen w-full items-center justify-center">
        <Loader2
          className="h-8 w-8 animate-spin text-muted-foreground"
          role="status"
          aria-live="polite"
        />
      </div>
    );
  }

  // If setup is needed, redirect to setup page with return location
  if (needsSetup === true) {
    return <Navigate to="/setup" state={{ from: location }} replace />;
  }

  // If not authenticated, redirect to login with return location
  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  // Forced password change: a flagged user must change their password before
  // reaching any other protected route. The change-password screen itself is
  // exempt; a non-flagged user who lands on it is sent home.
  const mustChangePassword = !!user?.must_change_password;
  const onChangePasswordRoute = location.pathname === "/change-password";
  if (mustChangePassword && !onChangePasswordRoute) {
    return <Navigate to="/change-password" replace />;
  }
  if (!mustChangePassword && onChangePasswordRoute) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}
