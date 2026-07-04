import { useState } from "react";
import { KeyRound, Loader2 } from "lucide-react";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { User } from "./types";

interface ResetPasswordDialogProps {
  open: boolean;
  user: User | null;
  onResetPassword: (user: User, newPassword: string) => Promise<void>;
  onOpenChange: (open: boolean) => void;
  onClose?: () => void;
}

export function ResetPasswordDialog({
  open,
  user,
  onResetPassword,
  onOpenChange,
  onClose,
}: ResetPasswordDialogProps) {
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [isResetting, setIsResetting] = useState(false);

  const handleReset = async () => {
    if (!user) return;
    if (newPassword !== confirmPassword) {
      toast.error("Passwords do not match");
      return;
    }
    if (newPassword.length < 8) {
      toast.error("Password must be at least 8 characters");
      return;
    }
    setIsResetting(true);
    try {
      await onResetPassword(user, newPassword);
      setNewPassword("");
      setConfirmPassword("");
    } finally {
      setIsResetting(false);
    }
  };

  const handleOpenChange = (open: boolean) => {
    if (!open) {
      setNewPassword("");
      setConfirmPassword("");
      onClose?.();
    }
    onOpenChange(open);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent aria-labelledby="password-title" aria-describedby="password-desc">
        <DialogHeader>
          <DialogTitle id="password-title" className="flex items-center gap-2">
            <KeyRound className="w-5 h-5" />
            Reset Password
          </DialogTitle>
          <DialogDescription id="password-desc">
            Set a new password for <strong>{user?.username}</strong>.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-4">
          <div className="space-y-2">
            <Label htmlFor="new-password">New Password</Label>
            <Input
              id="new-password"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="Enter new password"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="confirm-password">Confirm Password</Label>
            <Input
              id="confirm-password"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="Confirm new password"
            />
          </div>
          <p className="text-sm text-muted-foreground">
            User will be required to change their password on next login.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={isResetting}>
            Cancel
          </Button>
          <Button onClick={handleReset} disabled={isResetting}>
            {isResetting ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Resetting...
              </>
            ) : (
              "Reset Password"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
