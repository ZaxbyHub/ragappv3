import { useCallback, useEffect, useRef, useState } from "react";
import type { Document } from "@/lib/api/core";
import { listDocuments, listReindexJobs } from "@/lib/api/core";
import { listWikiJobs } from "@/lib/api/wiki";
import { listKMSJobs } from "@/lib/api/kms";
import { listDrafts, listDraftJobs } from "@/lib/api/draftRoom";
import { documentProgress } from "@/components/documents/documentProgress";
import { useVaultStore } from "@/stores/useVaultStore";
import {
  captureAuthPrincipalGeneration,
  captureAuthPrincipalRole,
} from "@/lib/api/auth-lifecycle";

/**
 * Shared job-family contract for background-job polling (issue #783).
 *
 * The five server-side job families the Activity center ([Workstream M] PR 4,
 * #784) surfaces in one tray. The KMS recompile polling (KMSPage) consumes
 * this module today; the tray consumes the same contract for every family
 * instead of growing per-page copies.
 */
export const JOB_FAMILIES = [
  "ingest",
  "wiki",
  "draft-room",
  "kms",
  "reindex",
] as const;

export type JobFamily = (typeof JOB_FAMILIES)[number];

/** Statuses a job of any family never leaves once reached. */
const TERMINAL_JOB_STATUSES = [
  "completed",
  "failed",
  "cancelled",
] as const;

export type TerminalJobStatus = (typeof TERMINAL_JOB_STATUSES)[number];

export function isTerminalJobStatus(
  status: string | null | undefined
): status is TerminalJobStatus {
  return (
    status === "completed" || status === "failed" || status === "cancelled"
  );
}

/** Minimal per-job view every family's fetcher must produce. */
export interface JobStatus {
  status: string;
  error?: string | null;
}

/**
 * Per-family polling defaults. Families differ in expected wall time: a KMS
 * vault recompile is short and interactive; an embedding reindex or an
 * ingest of a large document runs minutes.
 */
export const JOB_FAMILY_POLL_DEFAULTS: Record<
  JobFamily,
  { intervalMs: number; maxIterations: number }
> = {
  ingest: { intervalMs: 2000, maxIterations: 150 },
  wiki: { intervalMs: 2000, maxIterations: 150 },
  "draft-room": { intervalMs: 2000, maxIterations: 150 },
  kms: { intervalMs: 1000, maxIterations: 60 },
  reindex: { intervalMs: 2000, maxIterations: 300 },
};

export interface UseJobStatusOptions {
  /**
   * Poll-time fetcher for the watched job; return null when the job is not
   * (yet) visible. Constraint (issue #783): the underlying API client is
   * dereferenced ONLY here — never at render/hook-setup time — so suites
   * mocking the api module with partial factories never hit an
   * undefined-member access outside a poll.
   */
  fetchJob: () => Promise<JobStatus | null>;
  /** Called exactly once when a poll observes a terminal status. */
  onTerminal: (job: JobStatus) => void;
  /** Called when the iteration cap is reached without a terminal status. */
  onTimeout?: () => void;
  intervalMs?: number;
  maxIterations?: number;
}

/**
 * Family-parameterized job polling: bounded, ref-tracked, unmount-safe.
 *
 * `start()` begins a setTimeout-chain loop (never setInterval, so each tick
 * reads the latest options via a ref and a slow fetch cannot pile up); the
 * loop aborts on `stop()`, on unmount, on a terminal status, or at the
 * iteration cap. A thrown fetch or a not-yet-visible job keeps polling
 * under the same cap — transient invisibility is not an error.
 */
export function useJobStatus(family: JobFamily, opts: UseJobStatusOptions) {
  const [active, setActive] = useState(false);
  const activeRef = useRef(false);
  const iterationRef = useRef(0);
  const optsRef = useRef(opts);
  optsRef.current = opts;
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Stable indirection so the loop body can schedule its own next tick
  // without a self-referencing useCallback (react-hooks/immutability).
  const tickRef = useRef<() => void>(() => {});

  const stop = useCallback(() => {
    activeRef.current = false;
    setActive(false);
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => stop, [stop]);

  useEffect(() => {
    const tick = async (): Promise<void> => {
      if (!activeRef.current) return;
      const {
        fetchJob,
        onTerminal,
        onTimeout,
        intervalMs,
        maxIterations,
      } = optsRef.current;
      const resolvedInterval =
        intervalMs ?? JOB_FAMILY_POLL_DEFAULTS[family].intervalMs;
      const resolvedCap =
        maxIterations ?? JOB_FAMILY_POLL_DEFAULTS[family].maxIterations;
      iterationRef.current += 1;
      if (iterationRef.current > resolvedCap) {
        activeRef.current = false;
        setActive(false);
        onTimeout?.();
        return;
      }
      let job: JobStatus | null;
      try {
        job = await fetchJob();
      } catch {
        job = null; // transient fetch failure keeps polling under the cap
      }
      if (!activeRef.current) return;
      if (job !== null && isTerminalJobStatus(job.status)) {
        activeRef.current = false;
        setActive(false);
        onTerminal(job);
        return;
      }
      timerRef.current = setTimeout(() => {
        tickRef.current();
      }, resolvedInterval);
    };
    tickRef.current = () => {
      void tick();
    };
  }, [family]);

  const start = useCallback(() => {
    if (activeRef.current) return;
    activeRef.current = true;
    iterationRef.current = 0;
    setActive(true);
    tickRef.current();
  }, []);

  return { active, start, stop };
}

// ---------------------------------------------------------------------------
// Activity tray aggregation (issue #784, [Workstream M] PR 4 of 4)
//
// The tray's data source: one poll loop over the five family adapters, each
// reusing the family's existing server list client (never the client upload
// store). Mirrors the single-job hook's discipline above: setTimeout chain,
// never setInterval; API clients are imported (dynamic) and dereferenced
// ONLY at poll time; every family failure degrades to zero rows for one
// tick instead of throwing (a 403 for feature/admin-gated families is an
// ordinary "not visible to this user", not an error).
// ---------------------------------------------------------------------------

/** One normalized job row the Activity tray renders, whatever the family. */
export interface ActivityJobRow {
  family: JobFamily;
  /** Stable React key (never rendered as text). */
  key: string;
  title: string;
  /** Display text derived from the family's own phase vocabulary. */
  phase: string;
  status: string;
  terminal: boolean;
  /** Cancel is offered for wiki / draft-room / ingest while non-terminal. */
  cancellable: boolean;
  /** Retry is offered for failed wiki / draft-room jobs. */
  retryable: boolean;
  jobId?: number;
  vaultId?: number;
  draftId?: number;
  fileId?: string;
}

const ACTIVITY_TRAY_INTERVAL_MS = 8000;

/**
 * Floor for caller-supplied intervals: a 0/negative intervalMs must not
 * become a setTimeout(fn, 0) self-rescheduling hot loop.
 */
const ACTIVITY_MIN_INTERVAL_MS = 1000;

/** Page budget for the ingest family's bounded pagination. */
const ACTIVITY_INGEST_PAGES = 3;
const ACTIVITY_INGEST_PAGE_SIZE = 200;

/** Bounded retention: all live rows plus the N most recent terminal rows. */
const ACTIVITY_TERMINAL_RETENTION = 3;

function retainRows(rows: ActivityJobRow[]): ActivityJobRow[] {
  const live = rows.filter((r) => !r.terminal);
  // Terminal rows arrive concatenated per parent (vault/draft), each parent
  // newest-first — slicing positionally would spend the whole budget on the
  // first parent's backlog and hide a just-failed job from every later
  // vault/draft. Family job ids are globally autoincrement, so descending id
  // is global recency.
  const terminal = rows
    .filter((r) => r.terminal)
    .sort((a, b) => (b.jobId ?? 0) - (a.jobId ?? 0))
    .slice(0, ACTIVITY_TERMINAL_RETENTION);
  return [...live, ...terminal];
}

async function accessibleVaultIds(): Promise<number[]> {
  const store = useVaultStore.getState();
  // The tray's first tick can race the shell's vault fetch; an empty store
  // must not silently stay empty — kick a load so a later tick sees the
  // vaults (the kick is a no-op while the store is already loading).
  if (store.vaults.length === 0 && !store.loading) {
    void Promise.resolve(store.fetchVaults()).catch(() => {});
  }
  return store.vaults.map((v: { id: number }) => v.id);
}

async function fetchIngestRows(): Promise<ActivityJobRow[]> {
  // The route filters by a single status value; union both live statuses and
  // dedupe by file id (a list source that ignores the filter must not
  // double-count a document). Pagination is bounded: a pathological backlog
  // must not fan out unbounded requests — documents beyond the page budget
  // are not surfaced (disclosed in the release note).
  const fetchStatusDocs = async (status: string): Promise<Document[]> => {
    const docs: Document[] = [];
    for (let page = 1; page <= ACTIVITY_INGEST_PAGES; page += 1) {
      const res = await listDocuments({ status, perPage: ACTIVITY_INGEST_PAGE_SIZE, page });
      docs.push(...res.documents);
      if (docs.length >= res.total) break;
    }
    return docs;
  };
  const [pending, processing] = await Promise.all([
    fetchStatusDocs("pending"),
    fetchStatusDocs("processing"),
  ]);
  const byId = new Map<string, Document>();
  for (const doc of [...pending, ...processing]) {
    byId.set(doc.id, doc);
  }
  return [...byId.values()].map((doc) => ({
    family: "ingest" as const,
    key: `ingest-${doc.id}`,
    title: doc.filename || `Document ${doc.id}`,
    phase: documentProgress(doc).label,
    status: (doc.metadata?.status as string | undefined) ?? "",
    terminal: false,
    cancellable: true,
    retryable: false,
    fileId: doc.id,
  }));
}

async function fetchWikiRows(): Promise<ActivityJobRow[]> {
  const vaultIds = await accessibleVaultIds();
  const lists = await Promise.allSettled(
    vaultIds.map((vaultId) => listWikiJobs({ vault_id: vaultId }))
  );
  const jobs = lists.flatMap((r) => (r.status === "fulfilled" ? r.value.jobs : []));
  return retainRows(
    jobs.map((job) => ({
      family: "wiki" as const,
      key: `wiki-${job.id}`,
      // A failed compile's own error text is the most useful title the
      // family's job shape carries (WikiCompileJob has no title field).
      title:
        job.status === "failed" && job.error ? job.error : "Wiki compile",
      phase: job.status,
      status: job.status,
      terminal: isTerminalJobStatus(job.status),
      cancellable: !isTerminalJobStatus(job.status),
      retryable: job.status === "failed",
      jobId: job.id,
      vaultId: job.vault_id,
    }))
  );
}

async function fetchKmsRows(): Promise<ActivityJobRow[]> {
  const vaultIds = await accessibleVaultIds();
  // require_kms_enabled / per-vault read denials (403) are the family's
  // ordinary hidden state, not errors.
  const lists = await Promise.allSettled(
    vaultIds.map((vaultId) => listKMSJobs(vaultId))
  );
  const jobs = lists.flatMap((r) => (r.status === "fulfilled" ? r.value.jobs : []));
  return retainRows(
    jobs.map((job) => ({
      family: "kms" as const,
      key: `kms-${job.id}`,
      title: "KMS recompile",
      phase: job.status,
      status: job.status,
      terminal: isTerminalJobStatus(job.status),
      cancellable: false,
      retryable: false,
      jobId: job.id,
      vaultId: job.vault_id,
    }))
  );
}

async function fetchDraftRoomRows(): Promise<ActivityJobRow[]> {
  const drafts = await listDrafts({ page: 1, per_page: 10 });
  const lists = await Promise.allSettled(
    drafts.items.map((draft) => listDraftJobs(draft.id, { page: 1, per_page: 20 }))
  );
  const jobsByDraft = drafts.items.map((draft, i) => ({
    draft,
    jobs: lists[i].status === "fulfilled" ? lists[i].value.items : [],
  }));
  return retainRows(
    jobsByDraft.flatMap(({ draft, jobs }) =>
      jobs.map((job) => ({
        family: "draft-room" as const,
        key: `draft-${draft.id}-job-${job.id}`,
        title: draft.title || `Draft ${draft.id}`,
        phase: job.active_stage ?? job.status,
        status: job.status,
        terminal: isTerminalJobStatus(job.status),
        cancellable: !isTerminalJobStatus(job.status),
        retryable: job.status === "failed",
        draftId: draft.id,
        jobId: job.id,
      }))
    )
  );
}

async function fetchReindexRows(): Promise<ActivityJobRow[]> {
  const { jobs } = await listReindexJobs();
  return retainRows(
    jobs.map((job) => ({
      family: "reindex" as const,
      key: `reindex-${job.id}`,
      title:
        job.vault_id != null
          ? `Embedding reindex (vault ${job.vault_id})`
          : "Embedding reindex",
      phase: job.status,
      status: job.status,
      terminal: isTerminalJobStatus(job.status),
      cancellable: false,
      retryable: false,
      jobId: job.id,
    }))
  );
}

// --- Reindex family visibility (issue #1039) -------------------------------
//
// GET /documents/reindex/jobs sits behind require_admin_role
// (backend/app/api/routes/documents.py): for a member/viewer session it can
// ONLY ever 403. Polling it every tray tick is pure waste the browser still
// logs as a failed network resource (100+ 403s measured in one member
// session, 2026-10-28). Two layers keep the family visible exactly where it
// belongs, without touching the other four families (#1039 AC3):
//
//   1. Role-aware skip: when the principal's role is known (the auth store
//      publishes it to auth-lifecycle), a non-admin never dispatches the
//      call — zero requests for the whole session.
//   2. First-403 session backoff: when the role is NOT known (auth still
//      hydrating, or a user-less session shape like single_admin), the
//      family is fetched once; a 403 arms a backoff keyed to the principal
//      generation and later ticks skip the dispatch. A principal change
//      (login-as switch without a reload) or a later role publication
//      overrides the backoff either way (an admin keeps the family; a
//      member stays hidden), so the family can never wedge into the wrong
//      visibility for a whole page session.
//
// Session-wide by design: the tray is remounted on route changes, and a
// per-mount flag would re-issue the dead request after every navigation.
const REINDEX_ADMIN_ROLES: ReadonlySet<string> = new Set(["admin", "superadmin"]);
let reindexHiddenBy403Generation: number | null = null;

function reindexFamilyVisible(): boolean {
  const role = captureAuthPrincipalRole();
  if (role !== null) return REINDEX_ADMIN_ROLES.has(role);
  const generation = captureAuthPrincipalGeneration();
  if (
    reindexHiddenBy403Generation !== null &&
    reindexHiddenBy403Generation !== generation
  ) {
    // The 403 belonged to a previous principal: the new one gets a fresh
    // chance (its own first 403 re-arms the backoff).
    reindexHiddenBy403Generation = null;
  }
  return reindexHiddenBy403Generation === null;
}

/**
 * HTTP status of a family fetch failure, in either error shape this repo's
 * API client produces: the response interceptor's normalized Error (top-level
 * `status`) or a raw AxiosError (`.response.status`). Mirrors the
 * shape-tolerance convention of `logApiFailure` (core.ts).
 */
function httpStatusOf(error: unknown): number | undefined {
  const candidate = error as {
    status?: number;
    response?: { status?: number };
    originalError?: { response?: { status?: number } };
  } | null;
  return (
    candidate?.status ??
    candidate?.response?.status ??
    candidate?.originalError?.response?.status
  );
}

async function fetchAllActivityRows(): Promise<ActivityJobRow[]> {
  const results = await Promise.allSettled([
    fetchIngestRows(),
    fetchWikiRows(),
    fetchKmsRows(),
    fetchDraftRoomRows(),
    // Adapter stays registered inline (issue #784 family census) but is
    // dispatched only when the family can be visible to this principal
    // (issue #1039): the spread keeps the census-visible `fetchReindexRows()`
    // call while gating it before any network work happens.
    ...(reindexFamilyVisible()
      ? [
          fetchReindexRows().catch((error: unknown) => {
            // A 403 is the admin gate answering: hide the family for the
            // rest of this principal's session so later ticks skip the
            // dispatch. Any other failure (network, 5xx) stays an ordinary
            // one-tick family miss and keeps polling.
            if (httpStatusOf(error) === 403) {
              reindexHiddenBy403Generation = captureAuthPrincipalGeneration();
            }
            return [];
          }),
        ]
      : []),
  ]);
  return results.flatMap((r) => (r.status === "fulfilled" ? r.value : []));
}

export interface UseActivityJobsOptions {
  intervalMs?: number;
}

/**
 * Server-sourced, shell-level job list for the Activity tray: polls the five
 * family adapters on one bounded setTimeout chain (never the client upload
 * store — a reload or another tab loses nothing). `refresh()` forces an
 * immediate extra tick (used after tray Cancel/Retry actions).
 */
export function useActivityJobs(options: UseActivityJobsOptions = {}) {
  const [rows, setRows] = useState<ActivityJobRow[]>([]);
  const [loading, setLoading] = useState(false);
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const mountedRef = useRef(false);
  const inFlightRef = useRef(false);
  // A refresh() landing while a tick is in flight must not be dropped: it
  // sets this flag and the in-flight chain runs one more pass when it settles
  // (PRR-004 / review F-005b).
  const pendingRef = useRef(false);
  // Loading is a first-load signal, not a per-poll spinner (PRR-032).
  const hasLoadedRef = useRef(false);
  // Generation-scoped scheduling: only the newest effect generation may keep
  // the timer chain alive, so a StrictMode remount's stale in-flight fetch
  // settles without arming a second chain (PRR-007 / review F-005a).
  const generationRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Stable indirection so the loop can schedule its own next tick without a
  // self-referencing useCallback (same pattern as useJobStatus above).
  const tickRef = useRef<() => void>(() => {});

  const runFetch = useCallback(async () => {
    if (inFlightRef.current) {
      pendingRef.current = true;
      return;
    }
    inFlightRef.current = true;
    if (mountedRef.current && !hasLoadedRef.current) setLoading(true);
    try {
      do {
        pendingRef.current = false;
        const next = await fetchAllActivityRows();
        if (mountedRef.current) {
          setRows(next);
          hasLoadedRef.current = true;
        }
      } while (pendingRef.current && mountedRef.current);
    } finally {
      inFlightRef.current = false;
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const generation = ++generationRef.current;
    const schedule = (): void => {
      timerRef.current = setTimeout(
        () => tickRef.current(),
        Math.max(
          optionsRef.current.intervalMs ?? ACTIVITY_TRAY_INTERVAL_MS,
          ACTIVITY_MIN_INTERVAL_MS
        )
      );
    };
    const tick = (): void => {
      // Backgrounded tab: skip the fetch but keep the chain alive at the same
      // cadence — the next visible tick catches up (PRR-008).
      if (typeof document !== "undefined" && document.hidden) {
        schedule();
        return;
      }
      void runFetch()
        .catch(() => {
          // fetchAllActivityRows never rejects (allSettled); this guard only
          // covers the setState bookkeeping above.
        })
        .finally(() => {
          if (!mountedRef.current || generationRef.current !== generation) return;
          schedule();
        });
    };
    tickRef.current = tick;
    tick();
    return () => {
      // Live-value read is the point of a generation guard: the cleanup must
      // see whether a NEWER effect generation has taken ownership.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      if (generationRef.current === generation) {
        mountedRef.current = false;
      }
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [runFetch]);

  const refresh = useCallback(() => {
    void runFetch().catch(() => {});
  }, [runFetch]);

  return { rows, loading, refresh };
}
