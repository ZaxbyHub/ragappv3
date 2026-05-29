import { useState, useEffect } from "react";
import { toast } from "sonner";
import { useAuthStore } from "@/stores/useAuthStore";
import { changePassword, listOrganizations, listVaults, type Organization, type Vault } from "@/lib/api";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockOrganizations, mockVaults } from "@/fixtures/vaults";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { User, Lock, Loader2, Save, Building2, Database } from "lucide-react";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";

type UserRole = "superadmin" | "admin" | "member" | "viewer";

const ROLE_LABELS: Record<UserRole, string> = {
  superadmin: "Super Admin",
  admin: "Admin",
  member: "Member",
  viewer: "Viewer",
};

function ProfilePageContent() {
  const testMode = useTestMode();
  const user = useAuthStore((state) => state.user);
  const updateProfile = useAuthStore((state) => state.updateProfile);

  const [fullName, setFullName] = useState(user?.full_name || "");
  const [updatingProfile, setUpdatingProfile] = useState(false);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [changingPassword, setChangingPassword] = useState(false);
  const [passwordError, setPasswordError] = useState("");

  const [orgs, setOrgs] = useState<Organization[]>(testMode ? mockOrganizations : []);
  const [vaults, setVaults] = useState<Vault[]>(testMode ? mockVaults : []);
  const [loadingAccess, setLoadingAccess] = useState(!testMode);

  useEffect(() => {
    if (testMode) return;
    setLoadingAccess(true);
    Promise.allSettled([listOrganizations(), listVaults()]).then(([orgResult, vaultResult]) => {
      if (orgResult.status === "fulfilled") setOrgs(orgResult.value);
      if (vaultResult.status === "fulfilled") setVaults(vaultResult.value.vaults ?? []);
      setLoadingAccess(false);
    });
  }, [testMode]);

  const handleUpdateProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!fullName.trim()) return;
    setUpdatingProfile(true);
    try {
      await updateProfile({ full_name: fullName.trim() });
      toast.success("Profile updated successfully");
    } catch (err) {
      toast.error("Failed to update profile");
    } finally {
      setUpdatingProfile(false);
    }
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setPasswordError("");
    
    if (newPassword.length < 8) {
      setPasswordError("Password must be at least 8 characters long");
      return;
    }
    
    if (newPassword !== confirmPassword) {
      setPasswordError("Passwords do not match");
      return;
    }
    
    if (!currentPassword) {
      setPasswordError("Current password is required");
      return;
    }
    
    setChangingPassword(true);
    try {
      await changePassword(currentPassword, newPassword);
      toast.success("Password changed successfully");
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to change password";
      toast.error(message);
    } finally {
      setChangingPassword(false);
    }
  };

  if (!user) {
    return (
      <div className="flex justify-center py-12" role="status" aria-live="polite">
        <Loader2 className="w-8 h-8 animate-spin text-muted-foreground" />
        <span className="sr-only">Loading profile</span>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <PageTitleHeader
          title="Profile"
          description="Manage your account settings"
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <User className="w-5 h-5" />Profile Information
          </CardTitle>
          <CardDescription>Update your personal information</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleUpdateProfile} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="username">Username</Label>
              <Input id="username" value={user.username} disabled aria-label="Username" className="bg-muted" />
              <p className="text-xs text-muted-foreground">Username cannot be changed</p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="full-name">Full Name</Label>
              <Input id="full-name" placeholder="Your full name..." value={fullName} onChange={(e) => setFullName(e.target.value)} disabled={updatingProfile} aria-label="Full name" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="role">Role</Label>
              <div>
                <Badge variant="secondary">{ROLE_LABELS[user.role]}</Badge>
              </div>
              <p className="text-xs text-muted-foreground">Role is managed by system administrators</p>
            </div>
            <div className="flex justify-end">
              <Button type="submit" disabled={updatingProfile || !fullName.trim() || fullName === user.full_name}>
                {updatingProfile ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Save className="w-4 h-4 mr-2" />}
                Save Changes
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Lock className="w-5 h-5" />Change Password
          </CardTitle>
          <CardDescription>Update your account password</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleChangePassword} className="space-y-4">
            {passwordError && (
              <div role="alert" aria-live="assertive" className="rounded-sm border border-destructive bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {passwordError}
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor="current-password">Current Password</Label>
              <Input id="current-password" type="password" placeholder="Enter current password..." value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} disabled={changingPassword} aria-label="Current password" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="new-password">New Password</Label>
              <Input id="new-password" type="password" placeholder="Enter new password..." value={newPassword} onChange={(e) => setNewPassword(e.target.value)} disabled={changingPassword} aria-label="New password" />
              <p className="text-xs text-muted-foreground">Must be at least 8 characters</p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm-password">Confirm New Password</Label>
              <Input id="confirm-password" type="password" placeholder="Confirm new password..." value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} disabled={changingPassword} aria-label="Confirm new password" />
            </div>
            <div className="flex justify-end">
              <Button
                type="submit"
                disabled={changingPassword || !currentPassword || !newPassword || !confirmPassword}
              >
                {changingPassword ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Lock className="w-4 h-4 mr-2" />}
                Change Password
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {/* Organization memberships */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Building2 className="w-5 h-5" />Organization Access
          </CardTitle>
          <CardDescription>Organizations you belong to</CardDescription>
        </CardHeader>
        <CardContent>
          {loadingAccess ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm"><Loader2 className="w-4 h-4 animate-spin" /> Loading...</div>
          ) : orgs.length === 0 ? (
            <p className="text-sm text-muted-foreground">No organization memberships found.</p>
          ) : (
            <ul className="space-y-2">
              {orgs.map((org) => (
                <li key={org.id} className="flex items-center gap-2 text-sm">
                  <Building2 className="w-4 h-4 text-muted-foreground flex-shrink-0" />
                  <span className="font-medium">{org.name}</span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* Accessible vaults */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database className="w-5 h-5" />Vault Access
          </CardTitle>
          <CardDescription>Knowledge vaults you can access</CardDescription>
        </CardHeader>
        <CardContent>
          {loadingAccess ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm"><Loader2 className="w-4 h-4 animate-spin" /> Loading...</div>
          ) : vaults.length === 0 ? (
            <p className="text-sm text-muted-foreground">No vaults accessible.</p>
          ) : (
            <ul className="space-y-2">
              {vaults.map((vault) => (
                <li key={vault.id} className="flex items-center gap-2 text-sm">
                  <Database className="w-4 h-4 text-muted-foreground flex-shrink-0" />
                  <span className="font-medium">{vault.name}</span>
                  {vault.file_count > 0 && (
                    <span className="text-xs text-muted-foreground">{vault.file_count} docs</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// Route-level ProtectedRoute in App.tsx already handles auth guard
export default function ProfilePage() {
  return <ProfilePageContent />;
}
