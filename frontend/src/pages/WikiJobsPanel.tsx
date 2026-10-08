import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
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
import { useAuthOwner } from "@/hooks/useAuthOwner";
import {
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  subscribeAuthPrincipal,
} from "@/lib/api/auth-lifecycle";

const STATUS_COLORS: Record<WikiCompileJob["status"], string> = {
  pending: "bg-muted text-muted-foreground",
  running: "bg-primary/10 text-primary",
  completed: "bg-success/10 text-success",
  failed: "bg-destructive/10 text-destructive",
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
  const owner = useAuthOwner();
  const principalGeneration = useSyncExternalStore(
    subscribeAuthPrincipal,
    captureAuthPrincipalGeneration,
    captureAuthPrincipalGeneration,
  );

  return (
    <WikiJobsPanelState
      key={`${vaultId}:${owner.id}:${principalGeneration}`}
      vaultId={vaultId}
      refreshSignal={refreshSignal}
      owner={owner}
      principalGeneration={principalGeneration}
    />
  );
}

type AuthOwnerLease = ReturnType<typeof useAuthOwner>;

interface WikiJobsPanelStateProps extends WikiJobsPanelProps {
  owner: AuthOwnerLease;
  principalGeneration: number;
}

interface EffectLease { id: number; active: boolean }
interface QueryContext { vaultId: number; statusFilter: string }
interface ReadToken { id: number; lease: EffectLease; queryContext: QueryContext }
interface MutationToken { id: number; lease: EffectLease }

function WikiJobsPanelState({
  vaultId,
  refreshSignal = 0,
  owner,
  principalGeneration,
}: WikiJobsPanelStateProps) {
  const [jobs, setJobs] = useState<WikiCompileJob[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [jobsPhase, setJobsPhase] = useState<"loading" | "ready" | "error">("loading");
  const [jobsContext, setJobsContext] = useState<QueryContext | null>(null);
  const [jobsDataContext, setJobsDataContext] = useState<QueryContext | null>(null);
  const [actionLoading, setActionLoading] = useState<number | null>(null);
  const effectLeaseRef = useRef<EffectLease | null>(null);
  const nextLeaseIdRef = useRef(0);
  const nextReadIdRef = useRef(0);
  const readTokenRef = useRef<ReadToken | null>(null);
  const loadingTokenRef = useRef<ReadToken | MutationToken | null>(null);
  const actionIntentRef = useRef(0);
  const actionGatesRef = useRef(new Map<number, MutationToken>());
  const actionLoadingTokenRef = useRef<MutationToken | null>(null);
  const recompileGateRef = useRef<MutationToken | null>(null);
  const queryContext = useMemo(() => ({ vaultId, statusFilter }), [vaultId, statusFilter]);
  const queryContextRef = useRef(queryContext);
  queryContextRef.current = queryContext;
  const refreshRef = useRef<() => Promise<void>>(async () => {});

  const mayPublish = useCallback(
    (lease: EffectLease) =>
      lease.active &&
      effectLeaseRef.current === lease &&
      isCurrentAuthOwner(owner) &&
      captureAuthPrincipalGeneration() === principalGeneration,
    [owner, principalGeneration],
  );

  const refresh = useCallback(async () => {
    const lease = effectLeaseRef.current;
    const capturedQuery = queryContext;
    if (!lease || !mayPublish(lease) || queryContextRef.current !== capturedQuery) return;
    const token: ReadToken = {
      id: ++nextReadIdRef.current,
      lease,
      queryContext: capturedQuery,
    };
    readTokenRef.current = token;
    loadingTokenRef.current = token;
    const currentRead = () =>
      mayPublish(lease) &&
      readTokenRef.current === token &&
      queryContextRef.current === token.queryContext;
    if (!currentRead()) return;
    setLoading((current) => currentRead() ? true : current);
    setJobsPhase((current) => currentRead() ? "loading" : current);
    setJobsContext((current) => currentRead() ? capturedQuery : current);
    try {
      const res = await listWikiJobs({ vault_id: vaultId, status: statusFilter || undefined });
      if (!Array.isArray(res?.jobs)) throw new Error("Jobs response was malformed");
      if (!currentRead()) return;
      setJobs((current) => currentRead() ? res.jobs : current);
      setJobsDataContext((current) => currentRead() ? capturedQuery : current);
      setJobsPhase((current) => currentRead() ? "ready" : current);
    } catch (e) {
      if (currentRead()) {
        setJobsPhase((current) => currentRead() ? "error" : current);
        toast.error(e instanceof Error ? e.message : "Failed to load jobs");
      }
    } finally {
      if (currentRead() && loadingTokenRef.current === token) {
        setLoading((current) => currentRead() && loadingTokenRef.current === token ? false : current);
      }
    }
  }, [mayPublish, queryContext, statusFilter, vaultId]);
  refreshRef.current = refresh;

  useEffect(() => {
    const lease: EffectLease = { id: ++nextLeaseIdRef.current, active: true };
    effectLeaseRef.current = lease;
    return () => {
      lease.active = false;
      if (effectLeaseRef.current === lease) effectLeaseRef.current = null;
      if (readTokenRef.current?.lease === lease) readTokenRef.current = null;
    };
  }, []);

  useEffect(() => {
    void refresh();
    return () => {
      if (readTokenRef.current?.queryContext === queryContext) readTokenRef.current = null;
    };
  }, [queryContext, refresh, refreshSignal]);

  async function handleRetry(jobId: number) {
    const lease = effectLeaseRef.current;
    if (!lease || !mayPublish(lease) || actionGatesRef.current.has(jobId)) return;
    const token: MutationToken = { id: ++actionIntentRef.current, lease };
    actionGatesRef.current.set(jobId, token);
    actionLoadingTokenRef.current = token;
    setActionLoading((current) => mayPublish(lease) && actionLoadingTokenRef.current === token ? jobId : current);
    try {
      if (!mayPublish(lease) || actionGatesRef.current.get(jobId) !== token) return;
      await retryWikiJob(jobId, vaultId);
      if (mayPublish(lease)) toast.success("Job queued for retry");
      if (mayPublish(lease)) await refreshRef.current();
    } catch (e) {
      if (mayPublish(lease)) toast.error(e instanceof Error ? e.message : "Retry failed");
    } finally {
      if (actionGatesRef.current.get(jobId) === token) {
        actionGatesRef.current.delete(jobId);
        if (actionLoadingTokenRef.current === token) {
          setActionLoading((current) =>
            mayPublish(lease) && actionLoadingTokenRef.current === token && current === jobId
              ? null
              : current,
          );
        }
      }
    }
  }

  async function handleCancel(jobId: number) {
    const lease = effectLeaseRef.current;
    if (!lease || !mayPublish(lease) || actionGatesRef.current.has(jobId)) return;
    const token: MutationToken = { id: ++actionIntentRef.current, lease };
    actionGatesRef.current.set(jobId, token);
    actionLoadingTokenRef.current = token;
    setActionLoading((current) => mayPublish(lease) && actionLoadingTokenRef.current === token ? jobId : current);
    try {
      if (!mayPublish(lease) || actionGatesRef.current.get(jobId) !== token) return;
      await cancelWikiJob(jobId, vaultId);
      if (mayPublish(lease)) toast.success("Job cancelled");
      if (mayPublish(lease)) await refreshRef.current();
    } catch (e) {
      if (mayPublish(lease)) toast.error(e instanceof Error ? e.message : "Cancel failed");
    } finally {
      if (actionGatesRef.current.get(jobId) === token) {
        actionGatesRef.current.delete(jobId);
        if (actionLoadingTokenRef.current === token) {
          setActionLoading((current) =>
            mayPublish(lease) && actionLoadingTokenRef.current === token && current === jobId
              ? null
              : current,
          );
        }
      }
    }
  }

  async function handleRecompile() {
    const lease = effectLeaseRef.current;
    if (!lease || !mayPublish(lease) || recompileGateRef.current) return;
    const token: MutationToken = { id: ++actionIntentRef.current, lease };
    recompileGateRef.current = token;
    loadingTokenRef.current = token;
    setLoading((current) => mayPublish(lease) && loadingTokenRef.current === token ? true : current);
    try {
      if (!mayPublish(lease) || recompileGateRef.current !== token) return;
      const res = await recompileVaultWiki(vaultId);
      if (mayPublish(lease)) toast.success(`Recompile job queued (id: ${res.job_id})`);
      if (mayPublish(lease)) await refreshRef.current();
    } catch (e) {
      if (mayPublish(lease)) toast.error(e instanceof Error ? e.message : "Recompile failed");
    } finally {
      if (recompileGateRef.current === token) {
        recompileGateRef.current = null;
        if (loadingTokenRef.current === token) {
          setLoading((current) =>
            mayPublish(lease) && loadingTokenRef.current === token && current ? false : current,
          );
        }
      }
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
            onValueChange={(v) => {
              const lease = effectLeaseRef.current;
              if (!lease || !mayPublish(lease)) return;
              const next = v === "all" ? "" : v;
              setStatusFilter((current) => mayPublish(lease) ? next : current);
            }}
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
          <Button variant="outline" size="sm" onClick={() => void refreshRef.current()} disabled={loading}>
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
      {jobsPhase === "error" && jobsContext === queryContext && (
        <div className="flex items-center justify-between gap-2 rounded border border-destructive/40 p-3 text-sm" role="alert">
          <span>Couldn&apos;t load wiki jobs.</span>
          <Button variant="outline" size="sm" onClick={() => void refreshRef.current()} disabled={loading}>
            Retry
          </Button>
        </div>
      )}

      {jobsPhase === "ready" && jobsContext === queryContext && jobs.length === 0 && (
        <EmptyState
          icon={ClipboardList}
          title="No jobs found"
          description="Jobs will appear here when wiki compilations are queued."
        />
      )}

      {jobsDataContext === queryContext && jobs.map((job) => (
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
