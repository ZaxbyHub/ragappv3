import { Navigate, useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/useAuthStore";
import { Loader2 } from "lucide-react";

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
  const { isAuthenticated, isLoading, isInitialized, needsSetup } = useAuthStore();

  // When testMode is enabled, auto-login with demo credentials so protected
  // routes are accessible without a real backend. Pages already use
  // useTestMode() to render mock data.
  if (testMode) {
    if (!isAuthenticated) {
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
    return <>{children}</>;
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

  return <>{children}</>;
}
