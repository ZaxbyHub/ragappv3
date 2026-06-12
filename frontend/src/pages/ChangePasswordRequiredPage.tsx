import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { changePassword } from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
export default function ChangePasswordRequiredPage() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (!currentPassword) {
      setError("Current password is required");
      return;
    }
    if (newPassword.length < 8) {
      setError("New password must be at least 8 characters long");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }

    setSubmitting(true);
    try {
      await changePassword(currentPassword, newPassword);
      // Clear the flag locally so ProtectedRoute lets the user through, then
      // refresh the user from the server (now reachable, flag cleared).
      const current = useAuthStore.getState().user;
      if (current) {
        useAuthStore.setState({ user: { ...current, must_change_password: false } });
      }
      try {
        await useAuthStore.getState().fetchMe();
      } catch {
        // Non-fatal: the local flag clear above already unblocks navigation.
      }
      navigate("/", { replace: true });
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to change password";
      setError(message);
    } finally {
      setSubmitting(false);
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
              />
              <p className="text-xs text-muted-foreground">
                Must be at least 8 characters, with one digit and one uppercase letter.
              </p>
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
