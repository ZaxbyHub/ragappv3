import { useEffect, useState, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { RefreshCw, RotateCcw, X, Loader2, ClipboardList } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";
import { toast } from "sonner";
import {
  listWikiJobs,
  retryWikiJob,
  cancelWikiJob,
  recompileVaultWiki,
  type WikiCompileJob,
} from "@/lib/api";

const STATUS_COLORS: Record<WikiCompileJob["status"], string> = {
  pending: "bg-muted text-muted-foreground",
  running: "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300",
  completed: "bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300",
  failed: "bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300",
  cancelled: "bg-muted text-muted-foreground line-through",
};

const TRIGGER_LABELS: Record<WikiCompileJob["trigger_type"], string> = {
  ingest: "Ingest",
  query: "Query",
  memory: "Memory",
  manual: "Manual",
  settings_reindex: "Reindex",
};

interface WikiJobsPanelProps {
  vaultId: number;
  // Incremented by the parent on receipt of a wiki SSE event so this panel
  // refetches without depending on a manual refresh button click.
  refreshSignal?: number;
}

export function WikiJobsPanel({ vaultId, refreshSignal = 0 }: WikiJobsPanelProps) {
  const [jobs, setJobs] = useState<WikiCompileJob[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listWikiJobs({ vault_id: vaultId, status: statusFilter || undefined });
      setJobs(res.jobs);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to load jobs");
    } finally {
      setLoading(false);
    }
  }, [vaultId, statusFilter]);

  useEffect(() => {
    refresh();
  }, [refresh, refreshSignal]);

  async function handleRetry(jobId: number) {
    setActionLoading(jobId);
    try {
      await retryWikiJob(jobId, vaultId);
      toast.success("Job queued for retry");
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Retry failed");
    } finally {
      setActionLoading(null);
    }
  }

  async function handleCancel(jobId: number) {
    setActionLoading(jobId);
    try {
      await cancelWikiJob(jobId, vaultId);
      toast.success("Job cancelled");
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Cancel failed");
    } finally {
      setActionLoading(null);
    }
  }

  async function handleRecompile() {
    setLoading(true);
    try {
      const res = await recompileVaultWiki(vaultId);
      toast.success(`Recompile job queued (id: ${res.job_id})`);
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Recompile failed");
    } finally {
      setLoading(false);
    }
  }

  function formatDate(iso: string | null) {
    if (!iso) return "—";
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  return (
    <div className="flex flex-col gap-3">
      {/* Toolbar */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h3 className="text-sm font-semibold shrink-0">Compile Jobs</h3>
        <div className="flex items-center gap-2">
          <Select
            value={statusFilter || "all"}
            onValueChange={(v) => setStatusFilter(v === "all" ? "" : v)}
          >
            <SelectTrigger className="text-xs h-8 w-[140px]">
              <SelectValue placeholder="All statuses" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              <SelectItem value="pending">Pending</SelectItem>
              <SelectItem value="running">Running</SelectItem>
              <SelectItem value="completed">Completed</SelectItem>
              <SelectItem value="failed">Failed</SelectItem>
              <SelectItem value="cancelled">Cancelled</SelectItem>
            </SelectContent>
          </Select>
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            {loading ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <RefreshCw className="w-3.5 h-3.5" />
            )}
          </Button>
          <Button variant="outline" size="sm" onClick={handleRecompile} disabled={loading}>
            <RotateCcw className="w-3.5 h-3.5 mr-1" />
            Recompile
          </Button>
        </div>
      </div>

      {/* Job list */}
      {jobs.length === 0 && !loading && (
        <EmptyState
          icon={ClipboardList}
          title="No jobs found"
          description="Jobs will appear here when wiki compilations are queued."
        />
      )}

      {jobs.map((job) => (
        <Card key={job.id} className="text-sm">
          <CardContent className="py-2 px-3 flex flex-col gap-1">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <span className="font-mono text-xs text-muted-foreground shrink-0">#{job.id}</span>
                <span className="font-medium truncate">
                  {TRIGGER_LABELS[job.trigger_type] ?? job.trigger_type}
                </span>
                {job.trigger_id && (
                  <span className="text-xs text-muted-foreground truncate">{job.trigger_id}</span>
                )}
              </div>
              <span
                className={`text-xs rounded-sm px-1.5 py-0.5 shrink-0 font-medium ${STATUS_COLORS[job.status] ?? ""}`}
              >
                {job.status}
              </span>
            </div>

            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>Created {formatDate(job.created_at)}</span>
              {job.completed_at && <span>Done {formatDate(job.completed_at)}</span>}
            </div>

            {job.error && (
              <p className="text-xs text-destructive truncate" title={job.error}>
                {job.error}
              </p>
            )}

            {/* PR C: optional curator summary, parsed from result_json. */}
            <CuratorSummary resultJson={job.result_json} />

            {/* Actions */}
            <div className="flex gap-1.5 mt-0.5">
              {job.status === "failed" && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-6 text-xs"
                  disabled={actionLoading === job.id}
                  onClick={() => handleRetry(job.id)}
                >
                  {actionLoading === job.id ? (
                    <Loader2 className="w-3 h-3 animate-spin mr-1" />
                  ) : (
                    <RotateCcw className="w-3 h-3 mr-1" />
                  )}
                  Retry
                </Button>
              )}
              {(job.status === "pending" || job.status === "running") && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-6 text-xs"
                  disabled={actionLoading === job.id}
                  onClick={() => handleCancel(job.id)}
                >
                  {actionLoading === job.id ? (
                    <Loader2 className="w-3 h-3 animate-spin mr-1" />
                  ) : (
                    <X className="w-3 h-3 mr-1" />
                  )}
                  Cancel
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

/**
 * PR C — curator summary block for a wiki compile job.
 *
 * Reads the optional ``curator`` block from the job's ``result_json``
 * and renders accepted / rejected / lint / errors counts. Tolerates
 * missing/malformed data; renders nothing when there is no curator
 * activity (curator was disabled for this trigger or short-circuited
 * before any output).
 */
function CuratorSummary({
  resultJson,
}: {
  resultJson: string | null | undefined;
}) {
  if (!resultJson) return null;
  let parsed: unknown;
  try {
    parsed =
      typeof resultJson === "string" ? JSON.parse(resultJson) : resultJson;
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const curator = (parsed as { curator?: unknown }).curator;
  if (!curator || typeof curator !== "object") return null;
  const c = curator as {
    accepted?: number;
    rejected?: number;
    lint?: number;
    errors?: unknown;
    calls?: number;
  };
  const accepted = Number(c.accepted ?? 0);
  const rejected = Number(c.rejected ?? 0);
  const lint = Number(c.lint ?? 0);
  const calls = Number(c.calls ?? 0);
  const errors = Array.isArray(c.errors) ? c.errors.length : 0;
  if (accepted + rejected + lint + calls + errors === 0) return null;
  return (
    <div className="flex flex-wrap gap-2 text-[11px] text-muted-foreground pt-0.5">
      <span title="LLM curator candidates accepted (with verified source quote)">
        Curator accepted: <strong className="text-foreground">{accepted}</strong>
      </span>
      <span title="Curator candidates rejected by quote/chunk verification">
        rejected: <strong className="text-foreground">{rejected}</strong>
      </span>
      <span title="Curator-derived lint findings (e.g. unsupported_claim)">
        lint: <strong className="text-foreground">{lint}</strong>
      </span>
      <span title="Curator transport / parse errors (job still completed)">
        errors:{" "}
        <strong className={errors ? "text-destructive" : "text-foreground"}>
          {errors}
        </strong>
      </span>
      <span title="HTTP calls issued to the curator">
        calls: <strong className="text-foreground">{calls}</strong>
      </span>
    </div>
  );
}
