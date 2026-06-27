import { useState, useEffect } from "react";
import { useNavigate, Navigate, Link, useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/useAuthStore";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Loader2 } from "lucide-react";
import { MeridianLogo } from "@/components/icons/MeridianLogo";
import { HugeiconsIcon } from "@hugeicons/react";
import { LockPasswordIcon, Login01Icon, User02Icon, ViewOffSlashIcon, ViewIcon } from "@hugeicons/core-free-icons";

const TEST_MODE = import.meta.env.VITE_TEST_MODE === "true";
const DEMO_USERNAME = import.meta.env.VITE_DEMO_USERNAME || "demo";
const DEMO_PASSWORD = import.meta.env.VITE_DEMO_PASSWORD || "demo123";

export default function LoginPage() {
  const [credentials, setCredentials] = useState({
    username: TEST_MODE ? DEMO_USERNAME : "",
    password: TEST_MODE ? DEMO_PASSWORD : "",
  });

  const [error, setError] = useState("");
  const [showPassword, setShowPassword] = useState(false);

  const { login, needsSetup, isLoading, authMode } = useAuthStore();

  const navigate = useNavigate();
  const location = useLocation();
  // RT-03 fix: restore return-to URL after login
  const returnTo = (location.state as { from?: { pathname: string } })?.from?.pathname || "/";

  // Initialize auth store on mount (skip in test mode)
  useEffect(() => {
    if (!TEST_MODE) {
      useAuthStore.getState().init();
    }
  }, []);

  // Show loading while checking setup status
  if (needsSetup === null) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background p-4">
        <div className="flex flex-col items-center gap-3">
          <Loader2 className="h-8 w-8 animate-spin text-primary" aria-hidden="true" />
          <p className="text-sm text-muted-foreground">Loading Meridian…</p>
        </div>
      </div>
    );
  }

  // Redirect to setup if needsSetup is true
  if (needsSetup === true) {
    return <Navigate to="/setup" replace />;
  }

  if (authMode === "single_admin") {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="space-y-1">
            <div className="flex items-center justify-center mb-2">
              <div className="flex flex-col items-center justify-center">
                <MeridianLogo className="size-20" />
                <span className="text-2xl font-bold text-primary font-electrolize tracking-tighter uppercase">
                  Meridian
                </span>
              </div>
            </div>
            <CardTitle className="text-2xl text-center">Single-Admin Mode</CardTitle>
            <CardDescription className="text-center">
              User accounts are disabled for this deployment.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground text-center">
              Access is controlled by the configured admin secret. Ask the operator to enable user accounts before signing in here.
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (!credentials.username.trim() || !credentials.password) {
      setError("Please enter both username and password");
      return;
    }

    try {
      if (TEST_MODE) {
        // In test mode, validate against env credentials instead of backend
        if (
          credentials.username === DEMO_USERNAME &&
          credentials.password === DEMO_PASSWORD
        ) {
          const role = import.meta.env.VITE_DEMO_ROLE || "superadmin";
          useAuthStore.setState({
            user: {
              id: 1,
              username: DEMO_USERNAME,
              full_name: import.meta.env.VITE_DEMO_FULL_NAME || "Demo User",
              role: role as "superadmin" | "admin" | "member" | "viewer",
              is_active: true,
            },
            accessToken: "demo-token",
            isAuthenticated: true,
            isInitialized: true,
            needsSetup: false,
            isLoading: false,
            authMode: "jwt",
          });
          navigate(returnTo, { replace: true });
          return;
        }
        throw new Error("Invalid demo credentials");
      }

      await login(credentials.username, credentials.password);
      navigate(returnTo, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    }
  };

  const handleCredentialsChange = (field: string) => (
    e: React.ChangeEvent<HTMLInputElement>
  ) => {
    setCredentials((prev) => ({ ...prev, [field]: e.target.value }));
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-1">
          <div className="flex items-center justify-center mb-2">
            <div className="flex flex-col items-center justify-center">
              <MeridianLogo className="size-20" />
              <span className="text-2xl font-bold text-primary font-electrolize tracking-tighter uppercase">
                Meridian
              </span>
            </div>
          </div>
          <CardTitle className="text-2xl text-center">Welcome Back</CardTitle>
          <CardDescription className="text-center">
            Enter your credentials to access
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="login-username">Username</Label>
              <div className="relative">
                <HugeiconsIcon strokeWidth={1.2} icon={User02Icon} className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="login-username"
                  type="text"
                  placeholder="Username"
                  value={credentials.username}
                  onChange={handleCredentialsChange("username")}
                  disabled={isLoading}
                  autoFocus
                  aria-required="true"
                  aria-describedby={error ? "login-error" : undefined}
                  aria-invalid={!!error}
                  className="pl-10"
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="login-password">Password</Label>
              <div className="relative">
                <HugeiconsIcon strokeWidth={1.2} icon={LockPasswordIcon} className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="login-password"
                  type={showPassword ? "text" : "password"}
                  placeholder="Password"
                  value={credentials.password}
                  onChange={handleCredentialsChange("password")}
                  disabled={isLoading}
                  aria-required="true"
                  aria-describedby={error ? "login-error" : undefined}
                  aria-invalid={!!error}
                  className="pl-10 pr-10"
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="absolute right-0.5 top-1/2 -translate-y-1/2"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                >
                  {showPassword ? <HugeiconsIcon strokeWidth={1.2} icon={ViewOffSlashIcon} className="h-4 w-4" /> : <HugeiconsIcon strokeWidth={1.2} icon={ViewIcon} className="h-4 w-4" />}
                </Button>
              </div>
            </div>

            {error && (
              <p id="login-error" role="alert" className="text-sm text-destructive text-center">{error}</p>
            )}

            <Button
              type="submit"
              className="w-full"
              disabled={isLoading || !credentials.username.trim() || !credentials.password}
            >
              {isLoading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Verifying...
                </>
              ) : (
                <>
                  <HugeiconsIcon strokeWidth={1.2} icon={Login01Icon} className="mr-2 h-4 w-4" />
                  Sign In
                </>
              )}
            </Button>
          </form>
        </CardContent>

        <CardFooter className="flex justify-center border-t pt-4">
          <p className="text-sm text-muted-foreground">
            Don't have an account?{" "}
            <Link
              to="/register"
              className="text-primary hover:underline font-medium"
            >
              Register
            </Link>
          </p>
        </CardFooter>
      </Card>
    </div>
  );
}
