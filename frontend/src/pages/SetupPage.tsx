import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/useAuthStore";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Database, Shield, User, Loader2, Eye, EyeOff } from "lucide-react";
import ModelEndpointStep from "@/components/setup/ModelEndpointStep";

// Step 1 = superadmin creation (unchanged); step 2 = chat endpoint
// selection (issue #622). The wizard stays on /setup until the operator
// saves or skips; navigate("/") happens on finish only.
type SetupStep = "account" | "models";

export default function SetupPage() {
  const [step, setStep] = useState<SetupStep>("account");
  const modelsHeadingRef = useRef<HTMLParagraphElement>(null);
  const [formData, setFormData] = useState({
    username: "",
    full_name: "",
    password: "",
    confirmPassword: "",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const { register, needsSetup, isLoading } = useAuthStore();
  const navigate = useNavigate();

  // Redirect to login if setup is already complete. Two guards keep the
  // just-registered superadmin on the wizard (issue #622): (1) step — the
  // redirect only applies before the model-endpoint step; (2) registeringRef
  // — register() flips needsSetup:false SYNCHRONOUSLY inside the submit
  // handler, so a render flush can observe (needsSetup=false, step=account)
  // BEFORE setStep("models") lands; the ref (set before the await) closes
  // that window. Direct visits to /setup post-setup still redirect.
  const registeringRef = useRef(false);
  useEffect(() => {
    if (needsSetup === false && step === "account" && !registeringRef.current) {
      navigate("/login", { replace: true });
    }
  }, [needsSetup, navigate, step]);

  // a11y (PRR-019): when the wizard step mounts, move focus to its heading
  // so keyboard/screen-reader users land in the new context instead of on
  // <body> (the account step's focused submit button was just unmounted).
  useEffect(() => {
    if (step === "models") {
      modelsHeadingRef.current?.focus();
    }
  }, [step]);

  const validateForm = (): boolean => {
    const newErrors: Record<string, string> = {};

    // Username validation
    if (!formData.username.trim()) {
      newErrors.username = "Username is required";
    } else if (formData.username.length < 3) {
      newErrors.username = "Username must be at least 3 characters";
    }

    // Password validation
    if (!formData.password) {
      newErrors.password = "Password is required";
    } else if (formData.password.length < 8) {
      newErrors.password = "Password must be at least 8 characters";
    }

    // Confirm password validation
    if (formData.password !== formData.confirmPassword) {
      newErrors.confirmPassword = "Passwords do not match";
    }

    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleChange = (field: string) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setFormData((prev) => ({ ...prev, [field]: e.target.value }));
    // Clear error for this field when user starts typing
    if (errors[field]) {
      setErrors((prev) => ({ ...prev, [field]: "" }));
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!validateForm()) {
      return;
    }

    // Set BEFORE awaiting register(): the store flip inside register() is
    // synchronous and must never be observed by the redirect effect while
    // this submission is in flight.
    registeringRef.current = true;
    try {
      await register(
        formData.username,
        formData.password,
        formData.full_name || undefined
      );
      // The user is authenticated as superadmin; continue to the chat
      // endpoint wizard (issue #622) instead of entering the app directly.
      setStep("models");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "";
      setError(msg || "Setup failed. Please try again.");
    }
  };

  // Show loading state while checking setup status
  if (needsSetup === null || needsSetup === undefined) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background p-4">
        <Card className="w-full max-w-md">
          <CardContent className="flex flex-col items-center justify-center py-12">
            <Loader2 className="h-8 w-8 animate-spin text-primary" aria-hidden="true" />
            <p className="mt-4 font-medium text-foreground">Verifying your instance</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Checking whether the database has been initialized.
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (step === "models") {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="space-y-1">
            {/* a11y (issue #622 / PRR-019): the step swap unmounts the
                focused submit button, so focus is programmatically moved to
                the new step's heading (tabIndex -1, outlined on focus). */}
            <CardTitle
              ref={modelsHeadingRef}
              tabIndex={-1}
              className="text-2xl text-center focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary"
            >
              Configure Chat Models
            </CardTitle>
            <CardDescription className="text-center">
              Point the app at your own inference endpoints — no model ships by
              default. You can change these later in Settings → Models.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ModelEndpointStep onFinish={() => navigate("/")} />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-1">
          <div className="flex items-center justify-center mb-2">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
              <Database className="h-6 w-6 text-primary" />
            </div>
          </div>
          <CardTitle className="text-2xl text-center">Initial Setup</CardTitle>
          <CardDescription className="text-center">
            Create the first superadmin account to get started
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="setup-username">Username</Label>
              <div className="relative">
                <User className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="setup-username"
                  type="text"
                  placeholder="Username (required)"
                  value={formData.username}
                  onChange={handleChange("username")}
                  disabled={isLoading}
                  // eslint-disable-next-line jsx-a11y-x/no-autofocus -- Intentional first-field focus for initial administrator setup.
                  autoFocus
                  aria-required="true"
                  aria-describedby={errors.username ? "setup-username-error" : undefined}
                  aria-invalid={!!errors.username}
                  className="pl-10"
                />
              </div>
              {errors.username && (
                <p id="setup-username-error" className="text-sm text-destructive">{errors.username}</p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="setup-fullname">Full name</Label>
              <div className="relative">
                <User className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="setup-fullname"
                  type="text"
                  placeholder="Full name (optional)"
                  value={formData.full_name}
                  onChange={handleChange("full_name")}
                  disabled={isLoading}
                  aria-required="false"
                  className="pl-10"
                />
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="setup-password">Password</Label>
              <div className="relative">
                <Shield className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="setup-password"
                  type={showPassword ? "text" : "password"}
                  placeholder="Password (min 8 characters)"
                  value={formData.password}
                  onChange={handleChange("password")}
                  disabled={isLoading}
                  aria-required="true"
                  aria-describedby={errors.password ? "setup-password-error" : undefined}
                  aria-invalid={!!errors.password}
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
                  {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </Button>
              </div>
              {errors.password && (
                <p id="setup-password-error" className="text-sm text-destructive">{errors.password}</p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="setup-confirm-password">Confirm Password</Label>
              <div className="relative">
                <Shield className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="setup-confirm-password"
                  type={showConfirmPassword ? "text" : "password"}
                  placeholder="Confirm password"
                  value={formData.confirmPassword}
                  onChange={handleChange("confirmPassword")}
                  disabled={isLoading}
                  aria-required="true"
                  aria-describedby={errors.confirmPassword ? "setup-confirm-password-error" : undefined}
                  aria-invalid={!!errors.confirmPassword}
                  className="pl-10 pr-10"
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="absolute right-0.5 top-1/2 -translate-y-1/2"
                  onClick={() => setShowConfirmPassword((v) => !v)}
                  aria-label={showConfirmPassword ? "Hide password confirmation" : "Show password confirmation"}
                >
                  {showConfirmPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </Button>
              </div>
              {errors.confirmPassword && (
                <p id="setup-confirm-password-error" className="text-sm text-destructive">{errors.confirmPassword}</p>
              )}
            </div>

            {error && (
              <p role="alert" className="text-sm text-destructive text-center">{error}</p>
            )}

            <Button
              type="submit"
              className="w-full"
              disabled={
                !formData.username.trim() ||
                !formData.password ||
                !formData.confirmPassword ||
                isLoading
              }
            >
              {isLoading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Creating Account...
                </>
              ) : (
                <>
                  <Shield className="mr-2 h-4 w-4" />
                  Create Superadmin Account
                </>
              )}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
