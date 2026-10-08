import { useState, useEffect, useRef, useCallback, useSyncExternalStore } from "react";
import { toast } from "sonner";
import { useAuthStore } from "@/stores/useAuthStore";
import {
  changePassword,
  listOrganizations,
  listSessions,
  listAccessibleVaults,
  revokeAllSessions,
  revokeSession,
  setJwtAccessToken,
  type Organization,
  type Session,
  type Vault,
} from "@/lib/api";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockOrganizations, mockVaults } from "@/fixtures/vaults";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { User, Lock, Loader2, Save, Building2, Database, Monitor, LogOut } from "lucide-react";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import { PasswordRequirements } from "@/components/shared/PasswordRequirements";
import {
  captureAuthOwner,
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  onAuthOwnerReplacement,
  subscribeAuthPrincipal,
  type AuthOwner,
} from "@/lib/api/auth-lifecycle";

type UserRole = "superadmin" | "admin" | "member" | "viewer";

const ROLE_LABELS: Record<UserRole, string> = {
  superadmin: "Super Admin",
  admin: "Admin",
  member: "Member",
  viewer: "Viewer",
};

function formatSessionDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

type ReadStatus = "loading" | "success" | "error";

type MutationToken = { key: string; owner: AuthOwner; principalGeneration: number; id: symbol };

function publishReturnedCredentials(result: unknown, current: () => boolean): boolean {
  if (!current()) return false;
  const accessToken = typeof result === "object" && result !== null
    ? (result as { access_token?: unknown }).access_token
    : undefined;
  if (typeof accessToken !== "string" || accessToken.length === 0) return current();
  if (!current()) return false;
  setJwtAccessToken(accessToken);
  if (!current()) return false;
  useAuthStore.setState({ accessToken });
  return current();
}

function ProfilePageContent() {
  const testMode = useTestMode();
  const user = useAuthStore((state) => state.user);
  const updateProfile = useAuthStore((state) => state.updateProfile);

  const ownerRef = useRef<AuthOwner | null>(null);
  if (ownerRef.current === null) ownerRef.current = captureAuthOwner();
  const principalGenerationRef = useRef(captureAuthPrincipalGeneration());
  const mountedRef = useRef(true);
  const owner = ownerRef.current;
  const principalGeneration = principalGenerationRef.current;
  const current = useCallback(
    () => mountedRef.current && isCurrentAuthOwner(owner) && captureAuthPrincipalGeneration() === principalGeneration,
    [owner, principalGeneration],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const [fullName, setFullName] = useState(user?.full_name || "");
  const [updatingProfile, setUpdatingProfile] = useState(false);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [changingPassword, setChangingPassword] = useState(false);
  const [passwordError, setPasswordError] = useState("");

  const [orgs, setOrgs] = useState<Organization[]>(testMode ? mockOrganizations : []);
  const [vaults, setVaults] = useState<Vault[]>(testMode ? mockVaults : []);
  const [orgStatus, setOrgStatus] = useState<ReadStatus>(testMode ? "success" : "loading");
  const [vaultStatus, setVaultStatus] = useState<ReadStatus>(testMode ? "success" : "loading");
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionStatus, setSessionStatus] = useState<ReadStatus>(testMode ? "success" : "loading");
  const [revokingSessionIds, setRevokingSessionIds] = useState<Set<string>>(() => new Set());
  const [revokingOthers, setRevokingOthers] = useState(false);

  // Keep the last admitted token after release so queued reducers still have
  // exact authority, while a newer admission can retire them synchronously.
  const mutationTokensRef = useRef(new Map<string, symbol>());
  const pendingMutationKeysRef = useRef(new Set<string>());
  const orgAttemptRef = useRef(0);
  const vaultAttemptRef = useRef(0);
  const sessionAttemptRef = useRef(0);

  const beginMutation = useCallback((key: string): MutationToken | null => {
    if (!current() || pendingMutationKeysRef.current.has(key)) return null;
    const token: MutationToken = { key, owner, principalGeneration, id: Symbol(key) };
    mutationTokensRef.current.set(key, token.id);
    pendingMutationKeysRef.current.add(key);
    return token;
  }, [current, owner, principalGeneration]);

  const isMutationOwnerCurrent = useCallback((token: MutationToken): boolean => (
    isCurrentAuthOwner(token.owner)
      && captureAuthPrincipalGeneration() === token.principalGeneration
      && mutationTokensRef.current.get(token.key) === token.id
  ), []);

  const isOperationCurrent = useCallback((token: MutationToken): boolean => (
    current() && token.owner === owner && token.principalGeneration === principalGeneration
      && mutationTokensRef.current.get(token.key) === token.id
  ), [current, owner, principalGeneration]);

  const loadOrganizations = useCallback(async (): Promise<boolean> => {
    if (!current()) return false;
    const attempt = orgAttemptRef.current + 1;
    orgAttemptRef.current = attempt;
    setOrgStatus((previous) => current() && orgAttemptRef.current === attempt ? "loading" : previous);
    try {
      const result = await listOrganizations();
      if (!current() || orgAttemptRef.current !== attempt) return false;
      if (!Array.isArray(result)) throw new Error("Invalid organization access response");
      setOrgs((previous) => current() && orgAttemptRef.current === attempt ? result : previous);
      setOrgStatus((previous) => current() && orgAttemptRef.current === attempt ? "success" : previous);
      return true;
    } catch {
      if (!current() || orgAttemptRef.current !== attempt) return false;
      setOrgStatus((previous) => current() && orgAttemptRef.current === attempt ? "error" : previous);
      return false;
    }
  }, [current]);

  const loadVaults = useCallback(async (): Promise<boolean> => {
    if (!current()) return false;
    const attempt = vaultAttemptRef.current + 1;
    vaultAttemptRef.current = attempt;
    setVaultStatus((previous) => current() && vaultAttemptRef.current === attempt ? "loading" : previous);
    try {
      const result = await listAccessibleVaults();
      if (!current() || vaultAttemptRef.current !== attempt) return false;
      const values = (result as { vaults?: unknown } | null | undefined)?.vaults;
      if (!Array.isArray(values)) throw new Error("Invalid vault access response");
      setVaults((previous) => current() && vaultAttemptRef.current === attempt ? values as Vault[] : previous);
      setVaultStatus((previous) => current() && vaultAttemptRef.current === attempt ? "success" : previous);
      return true;
    } catch {
      if (!current() || vaultAttemptRef.current !== attempt) return false;
      setVaultStatus((previous) => current() && vaultAttemptRef.current === attempt ? "error" : previous);
      return false;
    }
  }, [current]);

  const loadSessions = useCallback(async (
    throwOnError = false,
    mutationToken?: MutationToken,
    showLoading = true,
  ): Promise<boolean> => {
    if (!current() || (mutationToken && !isOperationCurrent(mutationToken))) return false;
    const attempt = sessionAttemptRef.current + 1;
    sessionAttemptRef.current = attempt;
    if (showLoading) {
      setSessionStatus((previous) => current() && sessionAttemptRef.current === attempt ? "loading" : previous);
    }
    try {
      const result = await listSessions();
      if (!current() || sessionAttemptRef.current !== attempt || (mutationToken && !isOperationCurrent(mutationToken))) return false;
      const values = (result as { sessions?: unknown } | null | undefined)?.sessions;
      if (!Array.isArray(values)) throw new Error("Invalid sessions response");
      setSessions((previous) => current() && sessionAttemptRef.current === attempt
        && (!mutationToken || isOperationCurrent(mutationToken)) ? values as Session[] : previous);
      setSessionStatus((previous) => current() && sessionAttemptRef.current === attempt
        && (!mutationToken || isOperationCurrent(mutationToken)) ? "success" : previous);
      return true;
    } catch (error) {
      if (!current() || sessionAttemptRef.current !== attempt || (mutationToken && !isOperationCurrent(mutationToken))) return false;
      setSessionStatus((previous) => current() && sessionAttemptRef.current === attempt
        && (!mutationToken || isOperationCurrent(mutationToken)) ? "error" : previous);
      if (throwOnError) throw error;
      return false;
    }
  }, [current, isOperationCurrent]);

  const refreshSessions = useCallback((mutationToken?: MutationToken) => (
    loadSessions(true, mutationToken, false)
  ), [loadSessions]);

  useEffect(() => {
    if (!current()) return;
    if (testMode) {
      setOrgs(mockOrganizations);
      setVaults(mockVaults);
      setSessions([]);
      setOrgStatus("success");
      setVaultStatus("success");
      setSessionStatus("success");
      return;
    }
    void loadOrganizations();
    void loadVaults();
    void loadSessions();
  }, [current, loadOrganizations, loadSessions, loadVaults, testMode]);

  const handleUpdateProfile = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!current()) return;
    const profileName = fullName.trim();
    if (!profileName) return;
    const token = beginMutation("profile");
    if (!token) return;
    setUpdatingProfile((previous) => current() && isOperationCurrent(token) ? true : previous);
    try {
      await updateProfile({ full_name: profileName });
      if (!isMutationOwnerCurrent(token)) return;
      toast.success("Profile updated successfully");
      if (!isOperationCurrent(token)) return;
    } catch (err) {
      if (!isMutationOwnerCurrent(token)) return;
      toast.error("Failed to update profile");
    } finally {
      const admitted = mutationTokensRef.current.get(token.key) === token.id && current();
      if (admitted) {
        pendingMutationKeysRef.current.delete(token.key);
        setUpdatingProfile((previous) => isOperationCurrent(token) ? false : previous);
      }
    }
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!current()) return;
    const currentPasswordSnapshot = currentPassword;
    const newPasswordSnapshot = newPassword;
    const confirmPasswordSnapshot = confirmPassword;
    setPasswordError((previous) => current() ? "" : previous);
    if (newPasswordSnapshot.length < 8) {
      setPasswordError((previous) => current() ? "Password must be at least 8 characters long" : previous);
      return;
    }
    if (newPasswordSnapshot !== confirmPasswordSnapshot) {
      setPasswordError((previous) => current() ? "Passwords do not match" : previous);
      return;
    }
    if (!currentPasswordSnapshot) {
      setPasswordError((previous) => current() ? "Current password is required" : previous);
      return;
    }
    const token = beginMutation("password");
    if (!token) return;
    setChangingPassword((previous) => current() && isOperationCurrent(token) ? true : previous);
    try {
      const result: unknown = await changePassword(currentPasswordSnapshot, newPasswordSnapshot);
      if (!isOperationCurrent(token) || !publishReturnedCredentials(result, current)) return;
      if (!isOperationCurrent(token)) return;
      toast.success("Password changed successfully");
      if (!isOperationCurrent(token)) return;
      setCurrentPassword((previous) => isOperationCurrent(token) ? "" : previous);
      setNewPassword((previous) => isOperationCurrent(token) ? "" : previous);
      setConfirmPassword((previous) => isOperationCurrent(token) ? "" : previous);
    } catch (err: unknown) {
      if (!isOperationCurrent(token)) return;
      const message = err instanceof Error ? err.message : "Failed to change password";
      toast.error(message);
      if (!isOperationCurrent(token)) return;
    } finally {
      const admitted = mutationTokensRef.current.get(token.key) === token.id && current();
      if (admitted) {
        pendingMutationKeysRef.current.delete(token.key);
        setChangingPassword((previous) => isOperationCurrent(token) ? false : previous);
      }
    }
  };

  const handleRevokeSession = async (sessionId: string) => {
    if (!current()) return;
    const sessionIdSnapshot = sessionId;
    const token = beginMutation(`session:${sessionIdSnapshot}`);
    if (!token) return;
    setRevokingSessionIds((previous) => {
      if (!isOperationCurrent(token)) return previous;
      const next = new Set(previous);
      next.add(sessionIdSnapshot);
      return next;
    });
    try {
      await revokeSession(Number(sessionIdSnapshot));
      if (!isOperationCurrent(token)) return;
      await refreshSessions(token);
      if (!isOperationCurrent(token)) return;
      toast.success("Session revoked");
      if (!isOperationCurrent(token)) return;
    } catch (err: unknown) {
      if (!isOperationCurrent(token)) return;
      const message = err instanceof Error ? err.message : "Failed to revoke session";
      toast.error(message);
      if (!isOperationCurrent(token)) return;
    } finally {
      const admitted = mutationTokensRef.current.get(token.key) === token.id && current();
      if (admitted) {
        pendingMutationKeysRef.current.delete(token.key);
        setRevokingSessionIds((previous) => {
          if (!isOperationCurrent(token)) return previous;
          const next = new Set(previous);
          next.delete(sessionIdSnapshot);
          return next;
        });
      }
    }
  };

  const handleRevokeOtherSessions = async () => {
    if (!current()) return;
    const token = beginMutation("revoke-all");
    if (!token) return;
    setRevokingOthers((previous) => current() && isOperationCurrent(token) ? true : previous);
    try {
      const result: unknown = await revokeAllSessions();
      if (!isOperationCurrent(token) || !publishReturnedCredentials(result, current)) return;
      if (!isOperationCurrent(token)) return;
      await refreshSessions(token);
      if (!isOperationCurrent(token)) return;
      toast.success("Other sessions revoked");
      if (!isOperationCurrent(token)) return;
    } catch (err: unknown) {
      if (!isOperationCurrent(token)) return;
      const message = err instanceof Error ? err.message : "Failed to revoke other sessions";
      toast.error(message);
      if (!isOperationCurrent(token)) return;
    } finally {
      const admitted = mutationTokensRef.current.get(token.key) === token.id && current();
      if (admitted) {
        pendingMutationKeysRef.current.delete(token.key);
        setRevokingOthers((previous) => isOperationCurrent(token) ? false : previous);
      }
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
      <PageTitleHeader
        title="Profile"
        description="Manage your account settings"
      />

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
              <Input id="new-password" type="password" placeholder="Enter new password..." value={newPassword} onChange={(e) => setNewPassword(e.target.value)} disabled={changingPassword} aria-label="New password" aria-describedby="profile-password-requirements" />
              <PasswordRequirements id="profile-password-requirements" />
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

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Monitor className="w-5 h-5" />Active Sessions
          </CardTitle>
          <CardDescription>Review and revoke signed-in devices</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {sessionStatus === "loading" ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm"><Loader2 className="w-4 h-4 animate-spin" /> Loading...</div>
          ) : sessionStatus === "error" ? (
            <div className="space-y-2 text-sm text-destructive">
              <p>Unable to load sessions.</p>
              <Button type="button" variant="outline" onClick={() => { void loadSessions(); }}>Retry sessions</Button>
            </div>
          ) : sessions.length === 0 ? (
            <p className="text-sm text-muted-foreground">No active sessions found.</p>
          ) : (
            <div className="space-y-3">
              <div className="flex justify-end">
                <Button
                  type="button"
                  variant="outline"
                  onClick={handleRevokeOtherSessions}
                  disabled={revokingOthers || sessions.length <= 1}
                >
                  {revokingOthers ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <LogOut className="w-4 h-4 mr-2" />}
                  Sign Out Other Devices
                </Button>
              </div>
              <ul className="space-y-2">
                {sessions.map((session) => (
                  <li key={session.id} className="flex flex-col gap-3 rounded-md border p-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0 space-y-1 text-sm">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">{session.user_agent || "Unknown device"}</span>
                        {session.is_current && <Badge variant="secondary">Current</Badge>}
                      </div>
                      <p className="text-muted-foreground">
                        {session.ip_address || "Unknown IP"} · Created {formatSessionDate(session.created_at)} · Expires {formatSessionDate(session.expires_at)}
                      </p>
                    </div>
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => handleRevokeSession(session.id)}
                      disabled={session.is_current || revokingSessionIds.has(session.id)}
                      aria-label={session.is_current ? "Current session cannot be revoked here" : "Revoke session"}
                    >
                      {revokingSessionIds.has(session.id) ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <LogOut className="w-4 h-4 mr-2" />}
                      Revoke
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
          )}
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
          {orgStatus === "loading" ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm"><Loader2 className="w-4 h-4 animate-spin" /> Loading...</div>
          ) : orgStatus === "error" ? (
            <div className="space-y-2 text-sm text-destructive">
              <p>Unable to load organizations.</p>
              <Button type="button" variant="outline" onClick={() => { void loadOrganizations(); }}>Retry organization access</Button>
            </div>
          ) : orgs.length === 0 ? (
            <p className="text-sm text-muted-foreground">No organization memberships found.</p>
          ) : (
            <ul className="space-y-2">
              {orgs.map((org) => (
                <li key={org.id} className="flex items-center gap-2 text-sm">
                  <Building2 className="w-4 h-4 text-muted-foreground shrink-0" />
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
          {vaultStatus === "loading" ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm"><Loader2 className="w-4 h-4 animate-spin" /> Loading...</div>
          ) : vaultStatus === "error" ? (
            <div className="space-y-2 text-sm text-destructive">
              <p>Unable to load vault access.</p>
              <Button type="button" variant="outline" onClick={() => { void loadVaults(); }}>Retry vault access</Button>
            </div>
          ) : vaults.length === 0 ? (
            <p className="text-sm text-muted-foreground">No vaults accessible.</p>
          ) : (
            <ul className="space-y-2">
              {vaults.map((vault) => (
                <li key={vault.id} className="flex items-center gap-2 text-sm">
                  <Database className="w-4 h-4 text-muted-foreground shrink-0" />
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
const subscribeAuthOwner = (listener: () => void) => onAuthOwnerReplacement(() => listener());
const getAuthOwnerSnapshot = () => captureAuthOwner().id;

export default function ProfilePage() {
  const user = useAuthStore((state) => state.user);
  const ownerId = useSyncExternalStore(subscribeAuthOwner, getAuthOwnerSnapshot, getAuthOwnerSnapshot);
  const principalGeneration = useSyncExternalStore(
    subscribeAuthPrincipal,
    captureAuthPrincipalGeneration,
    captureAuthPrincipalGeneration,
  );
  const profileKey = `${ownerId}:${principalGeneration}:${user?.id ?? "none"}:${user?.role ?? "none"}`;
  return <ProfilePageContent key={profileKey} />;
}
