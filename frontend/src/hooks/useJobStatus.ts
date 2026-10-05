import { useCallback, useEffect, useRef, useState } from "react";

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
