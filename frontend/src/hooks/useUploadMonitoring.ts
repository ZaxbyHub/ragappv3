import { useEffect } from "react";
import { toast } from "sonner";
import * as apiModule from "@/lib/api";
import { getDocumentStatus } from "@/lib/api";
import type { DocumentStatusEntry, DocumentStatusesResponse } from "@/lib/api";
import { uploadNeedsMonitoring, useUploadStore } from "@/stores/useUploadStore";
import type { UploadFile, UploadStatusSnapshot } from "@/stores/useUploadStore";

/**
 * Shared upload monitoring (issue #514 / UI-042 / UPLOAD-DEEP-01).
 *
 * A refcounted singleton: while ANY consumer is mounted and ANY store upload
 * is non-terminal (`uploadNeedsMonitoring`), it issues exactly ONE batched
 * status request per tick (`getDocumentStatuses`) covering every monitored
 * document id, and applies the returned snapshots through
 * `applyStatusSnapshot` with a monotonically increasing attempt sequence —
 * so late, out-of-order responses can never regress newer state. Timers are
 * fully cancelled when the last consumer unmounts or nothing is left to
 * monitor (zero post-unmount requests); mounting the hook again resumes
 * monitoring from the preserved store state.
 *
 * Consumers: the chat Composer today; the documents page upload queue can
 * mount the same hook for identical behavior.
 */

// Tick cadence (1s) matches the composer attachment chip contract: the first
// status request fires one interval after an upload's bytes are accepted.
const BASE_POLL_INTERVAL_MS = 1000;
// Modest backoff while requests keep failing; resets on the first success.
const MAX_POLL_INTERVAL_MS = 8000;
const MAX_BACKOFF_STEPS = 3;
// Soft banner after 30 minutes, stop monitoring after 4 hours (the backend
// may still be working — never flip to error on a timeout).
const LONG_RUNNING_THRESHOLD_MS = 30 * 60 * 1000;
const MAX_MONITOR_DURATION_MS = 4 * 60 * 60 * 1000;

let refCount = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
let pollIntervalMs = BASE_POLL_INTERVAL_MS;
let consecutiveFailures = 0;
/** Monotonic attempt counter handed to applyStatusSnapshot for ordering. */
let attemptSeq = 0;

/**
 * Resolve the batched status client, or null when the api surface does not
 * provide it (an older module shape, or a module double that only defines a
 * subset of exports). Probed through the module namespace because accessing
 * a missing export can throw rather than yield undefined.
 */
function resolveBatchedStatusClient(): typeof apiModule.getDocumentStatuses | null {
  try {
    const client = (apiModule as Record<string, unknown>).getDocumentStatuses;
    return typeof client === "function"
      ? (client as typeof apiModule.getDocumentStatuses)
      : null;
  } catch {
    return null;
  }
}

function monitoringActive(): boolean {
  return (
    refCount > 0 && useUploadStore.getState().uploads.some(uploadNeedsMonitoring)
  );
}

function disarm(): void {
  if (timer != null) {
    clearTimeout(timer);
    timer = null;
  }
  pollIntervalMs = BASE_POLL_INTERVAL_MS;
  consecutiveFailures = 0;
}

function sync(): void {
  if (monitoringActive()) {
    if (timer == null) {
      timer = setTimeout(fire, pollIntervalMs);
    }
  } else {
    disarm();
  }
}

/** Time-based monitoring flags: long-running banner and the 4h hard cap. */
function applyMonitorDurationFlags(): void {
  const now = Date.now();
  let changed = false;
  const flagged = useUploadStore.getState().uploads.map((u) => {
    if (!uploadNeedsMonitoring(u) || u.startedAt == null) return u;
    const elapsedMs = now - u.startedAt;
    if (elapsedMs > MAX_MONITOR_DURATION_MS) {
      // Stop monitoring; do NOT mark error — the backend may still be working.
      changed = true;
      return { ...u, pollingStopped: true };
    }
    if (elapsedMs > LONG_RUNNING_THRESHOLD_MS && !u.longRunning) {
      changed = true;
      return { ...u, longRunning: true };
    }
    return u;
  });
  if (changed) useUploadStore.setState({ uploads: flagged });
}

function entryToSnapshot(entry: DocumentStatusEntry): UploadStatusSnapshot {
  if (entry.status) {
    return { ...entry, id: entry.id, status: entry.status };
  }
  // Per-id error entry (e.g. the id is unknown to this vault).
  return {
    id: entry.id,
    status: "error",
    error_message: entry.error ?? "Document status unavailable",
  };
}

interface TargetSnapshot {
  uploadId: string;
  snapshot: UploadStatusSnapshot;
}

async function fetchTargetSnapshots(
  targets: UploadFile[],
  vaultId: number | null
): Promise<TargetSnapshot[]> {
  const batchedClient = resolveBatchedStatusClient();
  if (batchedClient) {
    const documentIds = Array.from(
      new Set(targets.map((u) => u.documentId as string))
    );
    const response: DocumentStatusesResponse = await batchedClient(
      documentIds,
      vaultId ?? undefined
    );
    const uploadIdByDocumentId = new Map(
      targets.map((u) => [u.documentId as string, u.id])
    );
    const collected = response.results.map((entry) => {
      const uploadId = uploadIdByDocumentId.get(String(entry.id));
      return uploadId ? { uploadId, snapshot: entryToSnapshot(entry) } : null;
    });
    for (const batchError of response.errors ?? []) {
      if (batchError.id == null) continue;
      const uploadId = uploadIdByDocumentId.get(String(batchError.id));
      if (!uploadId) continue;
      collected.push({
        uploadId,
        snapshot: {
          id: batchError.id,
          status: "error",
          error_message: batchError.error,
        },
      });
    }
    return collected.filter((s): s is TargetSnapshot => s != null);
  }
  // Api surface without the batched client: fall back to one per-file status
  // request per monitored upload.
  return Promise.all(
    targets.map(async (u) => ({
      uploadId: u.id,
      snapshot: await getDocumentStatus(u.documentId as string),
    }))
  );
}

/** Announce (once) when an upload leaves monitoring in a terminal state. */
function announceTransitions(previousById: Map<string, UploadFile>): void {
  for (const u of useUploadStore.getState().uploads) {
    const prev = previousById.get(u.id);
    if (!prev || !uploadNeedsMonitoring(prev)) continue;
    if (uploadNeedsMonitoring(u)) continue;
    if (u.status === "indexed") {
      toast.success(`${u.file.name} indexed`);
    } else if (u.status === "error") {
      toast.error(`Failed to index ${u.file.name}`, {
        description: u.error ?? undefined,
      });
    }
  }
}

function fire(): void {
  timer = null;
  applyMonitorDurationFlags();
  const { uploads, activeVaultId } = useUploadStore.getState();
  const targets = uploads.filter(uploadNeedsMonitoring);
  if (targets.length === 0) return;

  // Re-arm BEFORE issuing the request: a slow (or held) response must never
  // stall the tick cadence — ordering of late responses is handled by the
  // attempt sequence in applyStatusSnapshot.
  timer = setTimeout(fire, pollIntervalMs);
  const seq = ++attemptSeq;
  const previousById = new Map(targets.map((u) => [u.id, u]));

  fetchTargetSnapshots(targets, activeVaultId)
    .then((snapshots) => {
      consecutiveFailures = 0;
      pollIntervalMs = BASE_POLL_INTERVAL_MS;
      for (const { uploadId, snapshot } of snapshots) {
        useUploadStore.getState().applyStatusSnapshot(uploadId, snapshot, seq);
      }
      announceTransitions(previousById);
    })
    .catch(() => {
      // Transient failure: ride through with a modest backoff. Monitoring
      // never flips an upload to error on network hiccups.
      consecutiveFailures += 1;
      pollIntervalMs = Math.min(
        BASE_POLL_INTERVAL_MS * 2 ** Math.min(consecutiveFailures, MAX_BACKOFF_STEPS),
        MAX_POLL_INTERVAL_MS
      );
    });
}

/**
 * Mount-driven monitoring: call from any surface that shows upload state
 * (Composer, documents page). The singleton runs while at least one
 * consumer is mounted; it stops requesting the moment the last one
 * unmounts, and resumes from preserved store state on the next mount.
 */
export function useUploadMonitoring(): void {
  const active = useUploadStore((s) => s.uploads.some(uploadNeedsMonitoring));

  useEffect(() => {
    refCount += 1;
    sync();
    return () => {
      refCount -= 1;
      sync();
    };
  }, []);

  useEffect(() => {
    sync();
  }, [active]);
}
