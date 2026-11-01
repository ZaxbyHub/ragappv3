import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useNavigate } from "react-router-dom";
import { changePassword, setJwtAccessToken } from "@/lib/api";
import {
  captureAuthOwner,
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  onAuthOwnerReplacement,
  subscribeAuthPrincipal,
} from "@/lib/api/auth-lifecycle";
import { useAuthStore } from "@/stores/useAuthStore";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PasswordRequirements } from "@/components/shared/PasswordRequirements";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Loader2 } from "lucide-react";
import { MeridianLogo } from "@/components/icons/MeridianLogo";

/**
 * Forced password-change screen. Shown to users flagged with
 * must_change_password (e.g. an admin-created account with a temporary
 * password). ProtectedRoute redirects flagged users here and blocks every other
 * route until the change succeeds, at which point the flag is cleared and the
 * user is sent into the app.
 */
function subscribePasswordScope(listener: () => void): () => void {
  const owner = onAuthOwnerReplacement(() => listener());
  const principal = subscribeAuthPrincipal(listener);
  return () => { owner(); principal(); };
}

function passwordScopeKey(): string {
  return `${captureAuthOwner().id}:${captureAuthPrincipalGeneration()}`;
}

export default function ChangePasswordRequiredPage() {
  const key = useSyncExternalStore(subscribePasswordScope, passwordScopeKey, passwordScopeKey);
  return <ChangePasswordRequiredContent key={key} />;
}

function ChangePasswordRequiredContent() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const ownerRef = useRef(captureAuthOwner());
  const principalRef = useRef(captureAuthPrincipalGeneration());
  const mountedRef = useRef(true);
  const latestIntentRef = useRef<symbol | null>(null);
  const pendingRef = useRef(false);
  const current = () => mountedRef.current && isCurrentAuthOwner(ownerRef.current)
    && captureAuthPrincipalGeneration() === principalRef.current;
  const owns = (intent: symbol) => current() && latestIntentRef.current === intent;

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!current() || pendingRef.current) return;
    const intent = Symbol("required-password");
    latestIntentRef.current = intent;
    const publishError = (message: string) => setError(previous => owns(intent) ? message : previous);
    publishError("");

    if (!currentPassword) {
      publishError("Current password is required");
      return;
    }
    if (newPassword.length < 8) {
      publishError("New password must be at least 8 characters long");
      return;
    }
    if (newPassword !== confirmPassword) {
      publishError("Passwords do not match");
      return;
    }

    pendingRef.current = true;
    setSubmitting(previous => owns(intent) ? true : previous);
    try {
      const result = await changePassword(currentPassword, newPassword);
      if (!owns(intent)) return;
      // Real responses contain rotated credentials; old void test doubles remain valid.
      const accessToken = result?.access_token;
      if (typeof accessToken === "string" && accessToken.length > 0) {
        setJwtAccessToken(accessToken);
        if (!owns(intent)) return;
        useAuthStore.setState({ accessToken });
        if (!owns(intent)) return;
      }
      // Preserve local flag clearing before the non-fatal user refresh.
      const currentUser = useAuthStore.getState().user;
      if (currentUser) {
        useAuthStore.setState({ user: { ...currentUser, must_change_password: false } });
      }
      if (!owns(intent)) return;
      try {
        await useAuthStore.getState().fetchMe();
      } catch {
        // The locally cleared flag still allows the existing successful navigation.
      }
      if (!owns(intent)) return;
      navigate("/", { replace: true });
    } catch (err: unknown) {
      if (!owns(intent)) return;
      publishError(err instanceof Error ? err.message : "Failed to change password");
    } finally {
      if (owns(intent)) {
        pendingRef.current = false;
        // Retain the last intent so a newer admission can retire this queued reducer.
        setSubmitting(previous => owns(intent) ? false : previous);
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
          <CardTitle className="text-2xl text-center">Set a new password</CardTitle>
          <CardDescription className="text-center">
            {user?.username
              ? `Welcome, ${user.username}. You must change your password before continuing.`
              : "You must change your password before continuing."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && (
              <div
                id="change-password-error"
                role="alert"
                aria-live="assertive"
                className="rounded-sm border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive"
              >
                {error}
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor="current-password">Current Password</Label>
              <Input
                id="current-password"
                type="password"
                placeholder="Enter current password..."
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                disabled={submitting}
                // eslint-disable-next-line jsx-a11y-x/no-autofocus -- Intentional first-field focus for the forced password-change flow.
                autoFocus
                aria-required="true"
                aria-invalid={!!error}
                aria-describedby={error ? "change-password-error" : undefined}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="new-password">New Password</Label>
              <Input
                id="new-password"
                type="password"
                placeholder="Enter new password..."
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                disabled={submitting}
                aria-required="true"
                aria-describedby="change-password-requirements"
              />
              <PasswordRequirements id="change-password-requirements" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm-password">Confirm New Password</Label>
              <Input
                id="confirm-password"
                type="password"
                placeholder="Confirm new password..."
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                disabled={submitting}
                aria-required="true"
              />
            </div>
            <Button
              type="submit"
              className="w-full"
              disabled={
                submitting ||
                !currentPassword ||
                !newPassword ||
                !confirmPassword
              }
            >
              {submitting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Updating...
                </>
              ) : (
                "Change password"
              )}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
