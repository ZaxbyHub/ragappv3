import { useCallback, useEffect, useRef, useState } from "react";
import type { Document } from "@/lib/api/core";
import type { ActivityJob } from "@/lib/api/activity";
import { getActivityJobs } from "@/lib/api";
import { documentProgress } from "@/components/documents/documentProgress";

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
// The tray's data source: one poll loop over ONE aggregate endpoint —
// getActivityJobs (GET /api/activity/jobs, audit 1115-1157-1158) — never
// the per-family fan-out and never the client upload store. Mirrors the
// single-job hook's discipline above: setTimeout chain, never setInterval;
// API clients are imported (dynamic) and dereferenced ONLY at poll time;
// a failed aggregate request retains the last good snapshot — there is no
// one-tick expiry, the snapshot is replaced only by a successful fetch (an
// authorization failure for content the caller cannot see is an ordinary
// "not visible to this user", not an error).
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

/**
 * Idle backoff (#1158 AC6): while the last snapshot had zero live rows the
 * chain stretches to 32s (~2 requests/min); any live row returns it to the
 * active 8s cadence (~8/min). Both stay under the ten-per-minute budget.
 */
const ACTIVITY_IDLE_INTERVAL_MS = 32000;

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

function ingestPhaseLabel(job: ActivityJob): string {
  // documentProgress reads its fields off a Document-shaped object (direct
  // fields first, metadata second) — the aggregate row already carries the
  // same phase columns, so a minimal carrier keeps the EXACT label logic.
  const carrier = {
    id: job.file_id ?? String(job.id),
    phase: job.phase ?? undefined,
    phase_message: job.phase_message ?? undefined,
    metadata: {
      status: job.status,
      progress_percent: job.progress_percent ?? undefined,
      processed_units: job.processed_units ?? undefined,
      total_units: job.total_units ?? undefined,
      unit_label: job.unit_label ?? undefined,
    },
  } as unknown as Document;
  return documentProgress(carrier).label;
}

function mapActivityJob(job: ActivityJob) {
  switch (job.family) {
    case "ingest":
      return {
        family: "ingest" as const,
        key: `ingest-${job.file_id ?? job.id}`,
        title: job.title,
        phase: ingestPhaseLabel(job),
        status: job.status,
        // The server only returns the two live ingest statuses, but the
        // classification stays status-derived, never hard-coded.
        terminal: isTerminalJobStatus(job.status),
        cancellable: !isTerminalJobStatus(job.status),
        retryable: false,
        fileId: job.file_id ?? undefined,
        vaultId: job.vault_id ?? undefined,
      };
    case "wiki":
      return {
        family: "wiki" as const,
        key: `wiki-${job.id}`,
        // A failed compile's own (already-redacted) error text is the most
        // useful title the family's job shape carries — same composition the
        // per-family adapter used.
        title: job.status === "failed" && job.error ? job.error : job.title,
        phase: job.status,
        status: job.status,
        terminal: isTerminalJobStatus(job.status),
        cancellable: !isTerminalJobStatus(job.status),
        retryable: job.status === "failed",
        jobId: job.id,
        vaultId: job.vault_id ?? undefined,
      };
    case "kms":
      return {
        family: "kms" as const,
        key: `kms-${job.id}`,
        title: job.title,
        phase: job.status,
        status: job.status,
        terminal: isTerminalJobStatus(job.status),
        cancellable: false,
        retryable: false,
        jobId: job.id,
        vaultId: job.vault_id ?? undefined,
      };
    case "draft-room":
      return {
        family: "draft-room" as const,
        key: `draft-${job.draft_id}-job-${job.id}`,
        title: job.title,
        phase: job.active_stage ?? job.status,
        status: job.status,
        terminal: isTerminalJobStatus(job.status),
        cancellable: !isTerminalJobStatus(job.status),
        retryable: job.status === "failed",
        draftId: job.draft_id ?? undefined,
        jobId: job.id,
        vaultId: job.vault_id ?? undefined,
      };
    case "reindex":
      return {
        family: "reindex" as const,
        key: `reindex-${job.id}`,
        title: job.title,
        phase: job.status,
        status: job.status,
        terminal: isTerminalJobStatus(job.status),
        cancellable: false,
        retryable: false,
        jobId: job.id,
      };
  }
}

async function fetchAllActivityRows(): Promise<ActivityJobRow[]> {
  // ONE request per tick (#1157's aggregate endpoint): the server scopes
  // families to exactly the vaults this caller can read (including the
  // #1039 admin-only reindex scoping and the draft-room owner boundary), so
  // the old per-family/per-vault fan-out and its client-side 403 backoff
  // layers are gone. Family failures are not per-family any more: a failed
  // aggregate call RETAINS the previous snapshot for that tick (last-known
  // state beats a blank tray); the next successful tick replaces it.
  const response = await getActivityJobs();
  const mapped = response.jobs.map(mapActivityJob);
  // Retention stays PER FAMILY (the <=3-terminal-rows budget each family's
  // adapter held): one global pass would spend the whole budget on a single
  // family's backlog and hide a just-failed job from every other family.
  const byFamily = new Map<string, ReturnType<typeof mapActivityJob>[]>();
  for (const row of mapped) {
    const list = byFamily.get(row.family) ?? [];
    list.push(row);
    byFamily.set(row.family, list);
  }
  return [...byFamily.values()].flatMap((familyRows) => retainRows(familyRows));
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
  // Idle backoff state (#1158 AC6): true while the last snapshot had at
  // least one live (non-terminal) row — the active 8s cadence; an all-idle
  // snapshot stretches the chain to ACTIVITY_IDLE_INTERVAL_MS.
  const hasLiveRef = useRef(false);
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
        // Queued-dispatch gate (closeout reviewer): a coalesced refresh can
        // be queued while visible and drained after the tab is hidden —
        // re-check visibility before EVERY dispatch, the same invariant as
        // the tick's and refresh's hidden gates. A stale pending flag is
        // harmless: it only ever causes one extra (visible) iteration later.
        if (typeof document !== "undefined" && document.hidden) {
          pendingRef.current = false;
          break;
        }
        pendingRef.current = false;
        const next = await fetchAllActivityRows();
        hasLiveRef.current = next.some((r) => !r.terminal);
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
      // Idle backoff (#1158 AC6): a caller-supplied intervalMs pins the
      // cadence (tests); otherwise the chain idles at 32s and runs at 8s
      // whenever the last snapshot held a live row.
      const base =
        optionsRef.current.intervalMs ??
        (hasLiveRef.current ? ACTIVITY_TRAY_INTERVAL_MS : ACTIVITY_IDLE_INTERVAL_MS);
      timerRef.current = setTimeout(() => tickRef.current(), Math.max(base, ACTIVITY_MIN_INTERVAL_MS));
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
          // fetchAllActivityRows rejects only when the aggregate request
          // itself fails; swallow it so the chain stays alive and the last
          // good snapshot remains on screen (r1-critic F-5 disposition).
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
    // Same hidden-tab gate as the scheduled tick (PRR-008): a user action is
    // started while the tab is visible, but its SETTLEMENT is asynchronous —
    // the finally-refresh must not dispatch a request once the document is
    // hidden. The chain's next visible tick reconciles the rows instead.
    if (typeof document !== "undefined" && document.hidden) return;
    void runFetch().catch(() => {});
  }, [runFetch]);

  return { rows, loading, refresh };
}
