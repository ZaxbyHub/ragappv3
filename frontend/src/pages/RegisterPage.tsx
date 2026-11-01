import { useNavigate, Link, Navigate } from "react-router-dom";
import { useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { useAuthOwner } from "@/hooks/useAuthOwner";
import { captureAuthPrincipalGeneration, subscribeAuthPrincipal } from "@/lib/api/auth-lifecycle";
import {
  canNavigateRegisterPublication,
  canReportRegisterFailure,
  captureRegisterPublicationScope,
  isRegisterPublicationAdmissionCurrent,
  type RegisterPublicationScope,
  useAuthStore,
  withRegisterPublicationScope,
} from "@/stores/useAuthStore";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PasswordRequirements } from "@/components/shared/PasswordRequirements";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Loader2 } from "lucide-react";
import { MeridianLogo } from "@/components/icons/MeridianLogo";
import { HugeiconsIcon } from "@hugeicons/react";
import { LockPasswordIcon, User02Icon, UserAdd01Icon, ViewIcon, ViewOffSlashIcon } from "@hugeicons/core-free-icons";

export default function RegisterPage() {
  type RegisterInvocation = { readonly token: symbol; readonly scope: RegisterPublicationScope };
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
  const { register, isLoading, isAuthenticated } = useAuthStore();
  const navigate = useNavigate();
  useAuthOwner();
  useSyncExternalStore(subscribeAuthPrincipal, captureAuthPrincipalGeneration, captureAuthPrincipalGeneration);
  const mountedRef = useRef(false);
  const activeInvocationRef = useRef<RegisterInvocation | null>(null);
  const latestInvocationRef = useRef<RegisterInvocation | null>(null);
  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      activeInvocationRef.current = null;
      latestInvocationRef.current = null;
    };
  }, []);
  // Intentionally render-local: a retained handler keeps A while a fresh render gets B.
  const renderScope = captureRegisterPublicationScope();
  const isRetiredInvocation = (scope: RegisterPublicationScope): boolean =>
    !isRegisterPublicationAdmissionCurrent(scope)
    && !canReportRegisterFailure(scope)
    && !canNavigateRegisterPublication(scope);
  const isLoadingForRegister = isLoading
    && (activeInvocationRef.current === null
      || !isRetiredInvocation(activeInvocationRef.current.scope));

  // Guard: redirect if already authenticated
  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  const validateForm = (isCurrent: () => boolean): boolean => {
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
    } else if (!/\d/.test(formData.password)) {
      newErrors.password = "Password must contain at least one digit";
    } else if (!/[A-Z]/.test(formData.password)) {
      newErrors.password = "Password must contain at least one uppercase letter";
    }

    // Confirm password validation
    if (formData.password !== formData.confirmPassword) {
      newErrors.confirmPassword = "Passwords do not match";
    }

    setErrors((previous) => isCurrent() ? newErrors : previous);
    return Object.keys(newErrors).length === 0;
  };

  const handleChange = (field: string) => (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    const isCurrent = () => mountedRef.current && isRegisterPublicationAdmissionCurrent(renderScope);
    if (!isCurrent()) return;
    setFormData((prev) => isCurrent() ? { ...prev, [field]: value } : prev);
    if (errors[field]) {
      setErrors((prev) => isCurrent() ? { ...prev, [field]: "" } : prev);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    const activeInvocation = activeInvocationRef.current;
    if (!mountedRef.current || !isRegisterPublicationAdmissionCurrent(renderScope)
      || (activeInvocation !== null
        && !(activeInvocation.scope !== renderScope && isRetiredInvocation(activeInvocation.scope)))) return;
    const invocation: RegisterInvocation = { token: Symbol("register"), scope: renderScope };
    latestInvocationRef.current = invocation;
    const isCurrentIntent = () => mountedRef.current && latestInvocationRef.current === invocation;
    const isCurrentValidation = () => isCurrentIntent() && !isRetiredInvocation(renderScope);
    if (!validateForm(isCurrentValidation) || !isCurrentIntent()
      || !isRegisterPublicationAdmissionCurrent(renderScope)) return;

    activeInvocationRef.current = invocation;
    try {
      await withRegisterPublicationScope(renderScope, () => register(
        formData.username,
        formData.password,
        formData.full_name || undefined,
      ));
      if (isCurrentIntent() && activeInvocationRef.current === invocation
        && canNavigateRegisterPublication(renderScope)) {
        navigate("/");
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "";
      const message = msg.includes("409") || msg.toLowerCase().includes("already")
        ? "Username already registered" : (msg || "Registration failed");
      if (isCurrentIntent() && activeInvocationRef.current === invocation
        && canReportRegisterFailure(renderScope)) {
        setError((previous) => isCurrentIntent() && canReportRegisterFailure(renderScope)
          ? message : previous);
      }
    } finally {
      if (activeInvocationRef.current === invocation) {
        activeInvocationRef.current = null;
      }
    }
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
          <CardTitle className="text-2xl text-center">Create Account</CardTitle>
          <CardDescription className="text-center">
            Sign up to start using Meridian
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="register-username">Username</Label>
              <div className="relative">
                <HugeiconsIcon strokeWidth={1.2} icon={User02Icon} className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
                <Input
                  id="register-username"
                  type="text"
                  placeholder="Username (required)"
                  value={formData.username}
                  onChange={handleChange("username")}
                  disabled={isLoadingForRegister}
                  // eslint-disable-next-line jsx-a11y-x/no-autofocus -- Intentional first-field focus for account registration.
                  autoFocus
                  aria-required="true"
                  aria-describedby={errors.username ? "register-username-error" : undefined}
                  aria-invalid={!!errors.username}
                  className="pl-10"
                />
              </div>
              {errors.username && (
                <p id="register-username-error" className="text-sm text-destructive">{errors.username}</p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="register-fullname">Full name</Label>
              <div className="relative">
                <HugeiconsIcon strokeWidth={1.2} icon={User02Icon} className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
                <Input
                  id="register-fullname"
                  type="text"
                  placeholder="Full name (optional)"
                  value={formData.full_name}
                  onChange={handleChange("full_name")}
                  disabled={isLoadingForRegister}
                  aria-required="false"
                  className="pl-10"
                />
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="register-password">Password</Label>
              {formData.password && <PasswordRequirements id="register-password-requirements" value={formData.password} />}
              <div className="relative">
                <HugeiconsIcon strokeWidth={1.2} icon={LockPasswordIcon} className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
                <Input
                  id="register-password"
                  type={showPassword ? "text" : "password"}
                  placeholder="Password (min 8 characters)"
                  value={formData.password}
                  onChange={handleChange("password")}
                  disabled={isLoadingForRegister}
                  aria-required="true"
                  aria-describedby={
                    formData.password
                      ? errors.password
                        ? "register-password-error register-password-requirements"
                        : "register-password-requirements"
                      : errors.password
                        ? "register-password-error"
                        : undefined
                  }
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
                  {showPassword ? <HugeiconsIcon strokeWidth={1.2} icon={ViewOffSlashIcon} className="h-4 w-4" aria-hidden="true" /> : <HugeiconsIcon strokeWidth={1.2} icon={ViewIcon} className="h-4 w-4" aria-hidden="true" />}
                </Button>
              </div>
              {errors.password && (
                <p id="register-password-error" className="text-sm text-destructive">{errors.password}</p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="register-confirm-password">Confirm Password</Label>
              <div className="relative">
                <HugeiconsIcon strokeWidth={1.2} icon={LockPasswordIcon} className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
                <Input
                  id="register-confirm-password"
                  type={showConfirmPassword ? "text" : "password"}
                  placeholder="Confirm password"
                  value={formData.confirmPassword}
                  onChange={handleChange("confirmPassword")}
                  disabled={isLoadingForRegister}
                  aria-required="true"
                  aria-describedby={errors.confirmPassword ? "register-confirm-password-error" : undefined}
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
                  {showConfirmPassword ? <HugeiconsIcon strokeWidth={1.2} icon={ViewOffSlashIcon} className="h-4 w-4" aria-hidden="true" /> : <HugeiconsIcon strokeWidth={1.2} icon={ViewIcon} className="h-4 w-4" aria-hidden="true" />}
                </Button>
              </div>
              {errors.confirmPassword && (
                <p id="register-confirm-password-error" className="text-sm text-destructive">{errors.confirmPassword}</p>
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
                isLoadingForRegister
              }
            >
              {isLoadingForRegister ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Creating Account...
                </>
              ) : (
                <>
                  <HugeiconsIcon strokeWidth={1.2} icon={UserAdd01Icon} className="mr-2 h-4 w-4" aria-hidden="true" />
                  Create Account
                </>
              )}
            </Button>
          </form>
        </CardContent>
        <CardFooter className="flex justify-center border-t pt-4">
          <p className="text-sm text-muted-foreground">
            Already have an account?{" "}
            <Link
              to="/login"
              className="text-primary hover:underline font-medium"
            >
              Sign in
            </Link>
          </p>
        </CardFooter>
      </Card>
    </div>
  );
}
