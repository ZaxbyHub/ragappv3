/**
 * Settings → Maintenance tab.
 *
 * Action buttons that hit existing backend endpoints only — nothing here is
 * decorative.
 *
 * Buttons:
 *   - Recompile wiki current vault   → POST /wiki/recompile
 *   - Run wiki lint                  → POST /wiki/lint/run
 *   - Test connections               → GET /settings/connection
 *   - Rebuild embeddings (all-vault reindex) → POST /documents/reindex
 *     (issue #833: the in-app recovery path for an embedding-mismatch 503
 *     after a runtime embedding-model change; always all-vault — a
 *     vault-scoped run restores readiness over a mixed index)
 *
 * Plus a "Recent jobs" mini-list using GET /wiki/jobs?limit=10 so the
 * operator can see the effect of a recompile without leaving the tab.
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { AlertTriangle, Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import {
  recompileVaultWiki,
  runWikiLint,
  listWikiJobs,
  testConnections,
  triggerDocumentReindex,
} from "@/lib/api";
import type { WikiCompileJob } from "@/lib/api";
import { useAuthOwner } from "@/hooks/useAuthOwner";
import {
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  subscribeAuthPrincipal,
} from "@/lib/api/auth-lifecycle";

export interface MaintenanceSettingsProps {
  /** Active vault id, used for wiki recompile/lint scoping. */
  vaultId: number | null;
}

export function MaintenanceSettings({ vaultId }: MaintenanceSettingsProps) {
  const owner = useAuthOwner();
  const principalGeneration = useSyncExternalStore(
    subscribeAuthPrincipal,
    captureAuthPrincipalGeneration,
    captureAuthPrincipalGeneration,
  );
  return (
    <MaintenanceSettingsConnectionActor
      key={`${owner.id}:${principalGeneration}`}
      vaultId={vaultId}
      owner={owner}
      principalGeneration={principalGeneration}
    />
  );
}

type AuthOwnerLease = ReturnType<typeof useAuthOwner>;

interface MaintenanceSettingsStateProps extends MaintenanceSettingsProps {
  owner: AuthOwnerLease;
  principalGeneration: number;
  connectionsBusy: boolean;
  onTestConnections: () => Promise<void>;
  connectionTokenRef: { current: ConnectionToken | null };
  vaultMutationTokenRef: { current: MutationToken | null };
}

interface EffectLease { id: number; active: boolean }
interface ReadToken { id: number; lease: EffectLease; queryKey: string }
interface MutationToken { id: number; lease: EffectLease }
interface ConnectionToken { id: number }

function MaintenanceSettingsConnectionActor({
  vaultId,
  owner,
  principalGeneration,
}: Omit<MaintenanceSettingsStateProps, "connectionsBusy" | "onTestConnections" | "connectionTokenRef" | "vaultMutationTokenRef">) {
  const [connectionsBusy, setConnectionsBusy] = useState(false);
  const activeRef = useRef(true);
  const nextConnectionIdRef = useRef(0);
  const connectionTokenRef = useRef<ConnectionToken | null>(null);
  const vaultMutationTokenRef = useRef<MutationToken | null>(null);
  const mayPublishConnections = useCallback(
    () =>
      activeRef.current &&
      isCurrentAuthOwner(owner) &&
      captureAuthPrincipalGeneration() === principalGeneration,
    [owner, principalGeneration],
  );

  useEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
      connectionTokenRef.current = null;
      vaultMutationTokenRef.current = null;
    };
  }, []);

  const handleTestConnections = useCallback(async () => {
    if (!mayPublishConnections() || connectionTokenRef.current || vaultMutationTokenRef.current) return;
    const token: ConnectionToken = { id: ++nextConnectionIdRef.current };
    connectionTokenRef.current = token;
    setConnectionsBusy(true);
    try {
      const out = await testConnections();
      if (mayPublishConnections() && connectionTokenRef.current === token) {
        const okCount = Object.values(out).filter((value) => value?.ok).length;
        toast.success(`Connection test ok for ${okCount}/${Object.keys(out).length} services`);
      }
    } catch (error) {
      if (mayPublishConnections() && connectionTokenRef.current === token) {
        toast.error(error instanceof Error ? error.message : "Failed to test connections");
      }
    } finally {
      if (connectionTokenRef.current === token) {
        connectionTokenRef.current = null;
        if (mayPublishConnections()) setConnectionsBusy(false);
      }
    }
  }, [mayPublishConnections]);

  return (
    <MaintenanceSettingsState
      key={`${vaultId}:${owner.id}:${principalGeneration}`}
      vaultId={vaultId}
      owner={owner}
      principalGeneration={principalGeneration}
      connectionsBusy={connectionsBusy}
      onTestConnections={handleTestConnections}
      connectionTokenRef={connectionTokenRef}
      vaultMutationTokenRef={vaultMutationTokenRef}
    />
  );
}

function MaintenanceSettingsState({
  vaultId,
  owner,
  principalGeneration,
  connectionsBusy,
  onTestConnections,
  connectionTokenRef,
  vaultMutationTokenRef,
}: MaintenanceSettingsStateProps) {
  const [busy, setBusy] = useState<
    null | "recompile" | "lint" | "reindex"
  >(null);
  const [recentJobs, setRecentJobs] = useState<WikiCompileJob[]>([]);
  const [jobsLoading, setJobsLoading] = useState(Boolean(vaultId));
  // True when the LAST jobs fetch failed (issue #774 / TQ-sibling-batch-08-04):
  // a failed load must not render "No recent jobs." — that is a verdict about
  // the job history, not about a failed request.
  const [jobsError, setJobsError] = useState(false);
  const effectLeaseRef = useRef<EffectLease | null>(null);
  const nextLeaseIdRef = useRef(0);
  const nextReadIdRef = useRef(0);
  const readTokenRef = useRef<ReadToken | null>(null);
  const mutationIntentRef = useRef(0);
  const mutationGatesRef = useRef(new Map<string, MutationToken>());
  const busyTokenRef = useRef<MutationToken | null>(null);
  const queryKey = String(vaultId);
  const queryKeyRef = useRef(queryKey);
  queryKeyRef.current = queryKey;
  const refreshRef = useRef<() => Promise<void>>(async () => {});

  const mayPublish = useCallback(
    (lease: EffectLease) =>
      lease.active &&
      effectLeaseRef.current === lease &&
      isCurrentAuthOwner(owner) &&
      captureAuthPrincipalGeneration() === principalGeneration,
    [owner, principalGeneration],
  );

  const refreshJobs = useCallback(async () => {
    const lease = effectLeaseRef.current;
    if (!lease || !mayPublish(lease)) return;
    const token: ReadToken = {
      id: ++nextReadIdRef.current,
      lease,
      queryKey,
    };
    readTokenRef.current = token;
    const currentRead = () =>
      mayPublish(lease) &&
      readTokenRef.current === token &&
      queryKeyRef.current === token.queryKey;
    if (!currentRead()) return;
    if (!vaultId) {
      setRecentJobs((current) => currentRead() ? [] : current);
      // Clearing the vault is not a failed load: drop any stale error so the
      // destructive banner cannot outlive the surface it described (PRR-004).
      setJobsError((current) => currentRead() ? false : current);
      setJobsLoading((current) => currentRead() ? false : current);
      return;
    }
    setJobsLoading((current) => currentRead() ? true : current);
    try {
      const out = await listWikiJobs({ vault_id: vaultId, limit: 10 });
      if (!Array.isArray(out?.jobs)) throw new Error("Jobs response was malformed");
      // Latest 10 by id desc — backend may already sort, but we don't rely.
      const jobs = out.jobs.slice(0, 10);
      if (!currentRead()) return;
      setRecentJobs((current) => currentRead() ? jobs : current);
      setJobsError((current) => currentRead() ? false : current);
    } catch (e) {
      // Request-level failure: keep the failure distinguishable from a
      // genuinely empty job history.
      if (currentRead()) {
        setJobsError((current) => currentRead() ? true : current);
      }
      void e;
    } finally {
      if (currentRead()) setJobsLoading((current) => currentRead() ? false : current);
    }
  }, [mayPublish, queryKey, vaultId]);
  refreshRef.current = refreshJobs;

  useEffect(() => {
    const lease: EffectLease = { id: ++nextLeaseIdRef.current, active: true };
    effectLeaseRef.current = lease;
    return () => {
      lease.active = false;
      if (effectLeaseRef.current === lease) effectLeaseRef.current = null;
      if (readTokenRef.current?.lease === lease) readTokenRef.current = null;
      if (vaultMutationTokenRef.current?.lease === lease) vaultMutationTokenRef.current = null;
    };
  }, [vaultMutationTokenRef]);

  useEffect(() => {
    void refreshJobs();
    return () => {
      if (readTokenRef.current?.queryKey === queryKey) readTokenRef.current = null;
    };
  }, [queryKey, refreshJobs]);

  const runMutation = useCallback(
    async (
      action: "recompile" | "lint" | "reindex",
      request: () => Promise<string>,
      failure: string,
      refreshAfter = false,
    ) => {
      const lease = effectLeaseRef.current;
      if (
        !lease ||
        !mayPublish(lease) ||
        mutationGatesRef.current.has(action) ||
        connectionTokenRef.current ||
        vaultMutationTokenRef.current
      ) return;
      const token: MutationToken = { id: ++mutationIntentRef.current, lease };
      mutationGatesRef.current.set(action, token);
      vaultMutationTokenRef.current = token;
      busyTokenRef.current = token;
      setBusy((current) => mayPublish(lease) && busyTokenRef.current === token ? action : current);
      try {
        if (!mayPublish(lease) || mutationGatesRef.current.get(action) !== token) return;
        const success = await request();
        if (mayPublish(lease)) toast.success(success);
        if (refreshAfter && mayPublish(lease)) await refreshRef.current();
      } catch (e) {
        if (mayPublish(lease)) toast.error(e instanceof Error ? e.message : failure);
      } finally {
        if (mutationGatesRef.current.get(action) === token) {
          mutationGatesRef.current.delete(action);
          if (vaultMutationTokenRef.current === token) vaultMutationTokenRef.current = null;
          if (busyTokenRef.current === token) {
            setBusy((current) =>
              mayPublish(lease) && busyTokenRef.current === token && current === action
                ? null
                : current,
            );
          }
        }
      }
    },
    [connectionTokenRef, mayPublish, vaultMutationTokenRef],
  );

  const handleRecompile = async () => {
    if (!vaultId) {
      const lease = effectLeaseRef.current;
      if (lease && mayPublish(lease)) toast.error("Select an active vault before recompiling.");
      return;
    }
    await runMutation("recompile", async () => {
      const out = await recompileVaultWiki(vaultId);
      if (!out?.job_id) throw new Error("Wiki recompile response was malformed");
      return `Wiki recompile queued (job ${out.job_id})`;
    }, "Failed to queue wiki recompile", true);
  };

  const handleLint = async () => {
    if (!vaultId) {
      const lease = effectLeaseRef.current;
      if (lease && mayPublish(lease)) toast.error("Select an active vault before running lint.");
      return;
    }
    await runMutation("lint", async () => {
      const out = await runWikiLint(vaultId);
      const count = out.count ?? out.findings?.length ?? 0;
      return `Wiki lint produced ${count} finding(s)`;
    }, "Failed to run wiki lint");
  };

  const [reindexConfirmOpen, setReindexConfirmOpen] = useState(false);

  const handleReindex = async () => {
    await runMutation("reindex", async () => {
      const out = await triggerDocumentReindex();
      if (!out?.job_id) throw new Error("Reindex response was malformed");
      return `Embedding reindex queued (job ${out.job_id}) — all vaults; recovery completes when the job finishes`;
    }, "Failed to queue embedding reindex");
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Maintenance actions</CardTitle>
          <CardDescription>
            Trigger wiki / lint / connection checks. Each button hits a real
            backend endpoint — nothing here is decorative.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={handleRecompile}
              disabled={busy !== null || connectionsBusy || !vaultId}
            >
              {busy === "recompile" && (
                <Loader2 className="w-4 h-4 mr-1 animate-spin" />
              )}
              Recompile wiki (current vault)
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={handleLint}
              disabled={busy !== null || connectionsBusy || !vaultId}
            >
              {busy === "lint" && (
                <Loader2 className="w-4 h-4 mr-1 animate-spin" />
              )}
              Run wiki lint
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onTestConnections}
              disabled={busy !== null || connectionsBusy}
            >
              {connectionsBusy && (
                <Loader2 className="w-4 h-4 mr-1 animate-spin" />
              )}
              Test connections
            </Button>
          </div>
          {!vaultId && (
            <p className="text-xs text-muted-foreground pt-2">
              Recompile and lint require an active vault. Pick one in the
              top-bar vault selector.
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Embeddings recovery</CardTitle>
          <CardDescription>
            After changing the embedding model (or an embedding prefix) in
            Models, search, chat, memories, and wiki pause with an
            embedding-mismatch 503 until an all-vault reindex runs to
            completion. Restarting does not clear the 503 — only a completed
            all-vault reindex restores readiness.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setReindexConfirmOpen(true)}
            disabled={busy !== null || connectionsBusy}
          >
            {busy === "reindex" && (
              <Loader2 className="w-4 h-4 mr-1 animate-spin" />
            )}
            Rebuild embeddings (all-vault reindex)
          </Button>
          <p className="text-xs text-muted-foreground">
            Re-embeds every indexed document across all vaults under the
            current embedding settings. Recovery from an embedding-mismatch
            503 requires this run to complete — the Activity tray shows the
            job&apos;s progress.
          </p>
        </CardContent>
      </Card>

      <Dialog
        open={reindexConfirmOpen}
        onOpenChange={(open) => {
          if (busy !== "reindex") setReindexConfirmOpen(open);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle
                className="h-5 w-5 text-warning"
                aria-hidden="true"
              />
              Rebuild all embeddings?
            </DialogTitle>
            <DialogDescription>
              This queues an all-vault reindex job that re-embeds every
              indexed document under the current embedding settings. It must
              run to completion to clear an embedding-mismatch 503 — partial
              or vault-scoped runs do not recover, and restarting the app
              does not clear the 503.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setReindexConfirmOpen(false)}
              disabled={busy === "reindex"}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setReindexConfirmOpen(false);
                void handleReindex();
              }}
              disabled={busy !== null}
            >
              {busy === "reindex" && (
                <Loader2 className="w-4 h-4 mr-1 animate-spin" />
              )}
              Start all-vault reindex
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle className="text-base">Recent wiki jobs</CardTitle>
            <CardDescription>
              Last 10 wiki compile jobs across the system.
            </CardDescription>
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={refreshJobs}
            disabled={jobsLoading}
            aria-label="Refresh wiki jobs"
          >
            {jobsLoading ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <RefreshCw className="w-4 h-4" />
            )}
          </Button>
        </CardHeader>
        <CardContent>
          {jobsError && (
            <div className="flex items-center justify-between gap-2 text-xs text-destructive" role="alert">
              <span>Couldn&apos;t load recent jobs</span>
              <Button variant="outline" size="sm" onClick={() => void refreshRef.current()} disabled={jobsLoading}>
                Retry
              </Button>
            </div>
          )}
          {jobsLoading && recentJobs.length === 0 ? (
            <p className="text-xs text-muted-foreground">Loading recent jobs…</p>
          ) : !jobsError && recentJobs.length === 0 ? (
            <p className="text-xs text-muted-foreground">No recent jobs.</p>
          ) : (
            <div className="space-y-1 text-xs">
              {recentJobs.map((job) => (
                <div
                  key={job.id}
                  className="flex items-center justify-between gap-2 border-b border-border/40 py-1 last:border-0"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-mono text-muted-foreground">
                      #{job.id}
                    </span>
                    <span className="truncate">{job.trigger_type}</span>
                    {job.trigger_id && (
                      <span className="truncate text-muted-foreground">
                        ({job.trigger_id})
                      </span>
                    )}
                  </div>
                  <span
                    className={
                      job.status === "completed"
                        ? "text-success"
                        : job.status === "failed"
                        ? "text-destructive"
                        : "text-muted-foreground"
                    }
                  >
                    {job.status}
                  </span>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
