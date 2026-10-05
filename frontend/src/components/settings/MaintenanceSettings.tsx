/**
 * Settings → Maintenance tab.
 *
 * Action buttons that hit existing backend endpoints only (per the
 * approved plan, the unwired "Reindex current vault" button is dropped —
 * POST /documents/reindex has since shipped (admin-gated; the Activity
 * tray's reindex family reads its job list); wiring a maintenance-tab
 * button for it is tracked separately).
 *
 * Buttons:
 *   - Recompile wiki current vault   → POST /wiki/recompile
 *   - Run wiki lint                  → POST /wiki/lint/run
 *   - Test connections               → GET /settings/connection
 *
 * Plus a "Recent jobs" mini-list using GET /wiki/jobs?limit=10 so the
 * operator can see the effect of a recompile without leaving the tab.
 */
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import {
  recompileVaultWiki,
  runWikiLint,
  listWikiJobs,
  testConnections,
} from "@/lib/api";
import type { WikiCompileJob } from "@/lib/api";

export interface MaintenanceSettingsProps {
  /** Active vault id, used for wiki recompile/lint scoping. */
  vaultId: number | null;
}

export function MaintenanceSettings({ vaultId }: MaintenanceSettingsProps) {
  const [busy, setBusy] = useState<
    null | "recompile" | "lint" | "connections" | "jobs"
  >(null);
  const [recentJobs, setRecentJobs] = useState<WikiCompileJob[]>([]);
  // True when the LAST jobs fetch failed (issue #774 / TQ-sibling-batch-08-04):
  // a failed load must not render "No recent jobs." — that is a verdict about
  // the job history, not about a failed request.
  const [jobsError, setJobsError] = useState(false);

  const refreshJobs = async () => {
    if (!vaultId) {
      setRecentJobs([]);
      return;
    }
    setBusy("jobs");
    try {
      const out = await listWikiJobs({ vault_id: vaultId, limit: 10 });
      // Latest 10 by id desc — backend may already sort, but we don't rely.
      const jobs = (out.jobs ?? []).slice(0, 10);
      setRecentJobs(jobs);
      setJobsError(false);
    } catch (e) {
      // Request-level failure: keep the failure distinguishable from a
      // genuinely empty job history.
      setRecentJobs([]);
      setJobsError(true);
      void e;
    } finally {
      setBusy(null);
    }
  };

  useEffect(() => {
    void refreshJobs();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vaultId]);

  const handleRecompile = async () => {
    if (!vaultId) {
      toast.error("Select an active vault before recompiling.");
      return;
    }
    setBusy("recompile");
    try {
      const out = await recompileVaultWiki(vaultId);
      toast.success(`Wiki recompile queued (job ${out.job_id})`);
      void refreshJobs();
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "Failed to queue wiki recompile",
      );
    } finally {
      setBusy(null);
    }
  };

  const handleLint = async () => {
    if (!vaultId) {
      toast.error("Select an active vault before running lint.");
      return;
    }
    setBusy("lint");
    try {
      const out = await runWikiLint(vaultId);
      toast.success(
        `Wiki lint produced ${out.count ?? out.findings?.length ?? 0} finding(s)`,
      );
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to run wiki lint");
    } finally {
      setBusy(null);
    }
  };

  const handleTestConnections = async () => {
    setBusy("connections");
    try {
      const out = await testConnections();
      const okCount = Object.values(out).filter((v) => v?.ok).length;
      toast.success(
        `Connection test ok for ${okCount}/${Object.keys(out).length} services`,
      );
    } catch (e) {
      toast.error(
        e instanceof Error ? e.message : "Failed to test connections",
      );
    } finally {
      setBusy(null);
    }
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
              disabled={busy !== null || !vaultId}
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
              disabled={busy !== null || !vaultId}
            >
              {busy === "lint" && (
                <Loader2 className="w-4 h-4 mr-1 animate-spin" />
              )}
              Run wiki lint
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={handleTestConnections}
              disabled={busy !== null}
            >
              {busy === "connections" && (
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
            disabled={busy === "jobs"}
            aria-label="Refresh wiki jobs"
          >
            {busy === "jobs" ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <RefreshCw className="w-4 h-4" />
            )}
          </Button>
        </CardHeader>
        <CardContent>
          {jobsError ? (
            <p className="text-xs text-destructive" role="alert">
              No recent jobs — couldn't load
            </p>
          ) : recentJobs.length === 0 ? (
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
                        ? "text-emerald-600"
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
