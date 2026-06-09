import { create } from "zustand";
import { uploadDocument, getDocumentStatus } from "@/lib/api";
import type { DocumentStatusResponse } from "@/lib/api";
import { isUploadTooLarge, normalizeUploadErrorMessage, uploadSizeExceededMessage } from "@/lib/uploadLimits";
import { toast } from "sonner";

/**
 * UploadFile state machine
 * ------------------------
 *   pending -> uploading -> processing -> indexed | error | cancelled
 *
 * `progress` is preserved as a deprecated alias of `uploadProgress` for one
 * release so external readers (e.g. legacy tests, untouched components) keep
 * compiling. New code should read `uploadProgress` / `processingProgress` /
 * `wikiProgress` directly.
 *
 * Backend `phase` (queued / parsing / extracting_text / chunking / embedding
 * / writing_index / indexed / error) drives the user-facing `phaseLabel`. The
 * `status` field is intentionally coarse so downstream components can keep
 * using simple equality checks.
 */
export type UploadStatus =
  | "pending"
  | "uploading"
  | "processing"
  | "indexing"
  | "indexed"
  | "error"
  | "cancelled";

export interface UploadFile {
  id: string;
  file: File;
  /** Network upload progress, 0..100. */
  uploadProgress: number;
  /**
   * @deprecated Read `uploadProgress` instead. Kept as an alias so callers that
   * haven't migrated yet still see a sensible number; will be removed in a
   * future release.
   */
  progress: number;
  /** Server-side processing progress, 0..100. Null when phase is indeterminate. */
  processingProgress?: number | null;
  /** Wiki compile progress, 0..100. Null when no wiki job is active. */
  wikiProgress?: number | null;
  status: UploadStatus;
  error?: string;
  documentId?: string;
  /** Raw backend phase string, e.g. "embedding". */
  phase?: string | null;
  /** User-friendly label derived from `phase`. */
  phaseLabel?: string | null;
  /** Backend-supplied phase message. */
  phaseMessage?: string | null;
  processedUnits?: number | null;
  totalUnits?: number | null;
  unitLabel?: string | null;
  /** epoch ms when this upload was first added to the queue */
  startedAt?: number;
  /** epoch ms when the current phase began (best-effort, frontend clock) */
  phaseStartedAt?: number;
  /** Server-computed elapsed seconds since processing started. */
  elapsedSeconds?: number | null;
  /** Backend wiki status: pending | running | completed | failed | cancelled */
  wikiStatus?: string | null;
  /** True once the polling loop has decided to stop trying (manual stop or hard cap). */
  pollingStopped?: boolean;
  /** True once the long-running banner should be shown (>= 30 min processing). */
  longRunning?: boolean;
}

interface UploadState {
  uploads: UploadFile[];
  isProcessing: boolean;
  activeVaultId: number | null;

  // Actions
  addUploads: (files: File[], vaultId?: number) => void;
  cancelUpload: (id: string) => void;
  removeUpload: (id: string) => void;
  updateUploadProgress: (id: string, progress: number) => void;
  /** @deprecated alias of updateUploadProgress for legacy callers */
  updateProgress: (id: string, progress: number) => void;
  setStatus: (id: string, status: UploadStatus, error?: string) => void;
  applyStatusSnapshot: (id: string, snapshot: DocumentStatusResponse) => void;
  setProcessing: (processing: boolean) => void;
  clearCompleted: () => void;
  retryUpload: (id: string) => void;
  /** Stop polling for this upload without cancelling backend processing. */
  stopPolling: (id: string) => void;
  processQueue: () => Promise<void>;
}

// Phase wire-value -> user-facing label map. Keep in sync with
// backend `services/document_progress.py::ALL_PHASES`.
const PHASE_LABELS: Record<string, string> = {
  queued: "Queued",
  parsing: "Parsing",
  extracting_text: "Extracting text",
  chunking: "Chunking",
  embedding: "Embedding",
  writing_index: "Writing index",
  indexed: "Indexed",
  error: "Error",
};

export function phaseLabelFor(phase?: string | null): string | null {
  if (!phase) return null;
  return PHASE_LABELS[phase] ?? phase;
}

// Adaptive polling: 1.5s for first 20s, 3s for next 60s, 6s thereafter.
// Reset back to fast polling when the observed phase changes.
const MAX_POLL_DURATION_MS = 4 * 60 * 60 * 1000; // 4 hours hard cap
const LONG_RUNNING_THRESHOLD_MS = 30 * 60 * 1000; // 30 minutes

function pickPollDelay(elapsedMs: number): number {
  if (elapsedMs < 20_000) return 1500;
  if (elapsedMs < 80_000) return 3000;
  return 6000;
}

/**
 * Map a backend status snapshot onto our local UploadFile fields. We don't
 * touch fields the backend didn't supply so existing client state survives
 * partial responses.
 */
function snapshotToPatch(
  snapshot: DocumentStatusResponse,
  prevPhase?: string | null,
): Partial<UploadFile> {
  const patch: Partial<UploadFile> = {
    documentId: String(snapshot.id),
    phase: snapshot.phase ?? null,
    phaseLabel: phaseLabelFor(snapshot.phase),
    phaseMessage: snapshot.phase_message ?? null,
    processedUnits: snapshot.processed_units ?? null,
    totalUnits: snapshot.total_units ?? null,
    unitLabel: snapshot.unit_label ?? null,
    elapsedSeconds: snapshot.elapsed_seconds ?? null,
    wikiStatus: snapshot.wiki_status ?? null,
    error: snapshot.error_message ?? undefined,
  };

  if (snapshot.progress_percent != null) {
    patch.processingProgress = snapshot.progress_percent;
  } else if (
    snapshot.phase &&
    snapshot.phase !== "indexed" &&
    snapshot.phase !== "error"
  ) {
    // Indeterminate phase: keep null so the UI shows an indeterminate bar.
    patch.processingProgress = null;
  }

  if (snapshot.wiki_status === "running") {
    // Wiki compile doesn't expose granular percent today; render indeterminate.
    patch.wikiProgress = null;
  } else if (snapshot.wiki_status === "completed") {
    patch.wikiProgress = 100;
  } else {
    patch.wikiProgress = patch.wikiProgress ?? null;
  }

  // Refresh the phase-started-at clock when we observe a phase transition.
  if (snapshot.phase && snapshot.phase !== prevPhase) {
    patch.phaseStartedAt = Date.now();
  }

  // Status mapping: keep coarse for downstream eq-checks. The backend's
  // canonical 4-value `status` enum maps directly here. Phase string
  // independently drives the detailed UI.
  switch (snapshot.status) {
    case "indexed":
      patch.status = "indexed";
      break;
    case "error":
      patch.status = "error";
      break;
    case "processing":
    case "pending":
    default:
      patch.status = "processing";
      break;
  }

  return patch;
}

export const useUploadStore = create<UploadState>((set, get) => ({
  uploads: [],
  isProcessing: false,
  activeVaultId: null,

  addUploads: (files, vaultId) => {
    if (!vaultId) {
      toast.error("No vault selected. Please select a vault before uploading.");
      return;
    }
    const acceptedFiles = files.filter((file) => {
      if (!isUploadTooLarge(file)) return true;
      toast.error(uploadSizeExceededMessage(file.name));
      return false;
    });
    if (acceptedFiles.length === 0) return;
    const generateId = (f: File) => {
      if (typeof crypto !== "undefined" && crypto.randomUUID) {
        return crypto.randomUUID();
      }
      return `${f.name}-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    };

    const now = Date.now();
    const newUploads: UploadFile[] = acceptedFiles.map((file) => ({
      id: generateId(file),
      file,
      uploadProgress: 0,
      progress: 0,
      processingProgress: null,
      wikiProgress: null,
      status: "pending",
      startedAt: now,
    }));

    set((state) => ({
      uploads: [...state.uploads, ...newUploads],
      activeVaultId: vaultId || state.activeVaultId,
    }));

    const { isProcessing } = get();
    if (!isProcessing) {
      get().processQueue();
    }
  },

  cancelUpload: (id) => {
    set((state) => ({
      uploads: state.uploads.map((u) =>
        u.id === id && u.status === "pending" ? { ...u, status: "cancelled" } : u
      ),
    }));
    toast.info("Upload cancelled");
  },

  removeUpload: (id) => {
    set((state) => ({
      uploads: state.uploads.filter((u) => u.id !== id),
    }));
  },

  updateUploadProgress: (id, progress) => {
    set((state) => ({
      uploads: state.uploads.map((u) =>
        u.id === id ? { ...u, uploadProgress: progress, progress } : u
      ),
    }));
  },

  // Deprecated alias preserved so untouched callers still compile.
  updateProgress: (id, progress) => {
    get().updateUploadProgress(id, progress);
  },

  setStatus: (id, status, error) => {
    set((state) => ({
      uploads: state.uploads.map((u) =>
        u.id === id ? { ...u, status, error } : u
      ),
    }));
  },

  applyStatusSnapshot: (id, snapshot) => {
    set((state) => ({
      uploads: state.uploads.map((u) => {
        if (u.id !== id) return u;
        const patch = snapshotToPatch(snapshot, u.phase);
        return { ...u, ...patch };
      }),
    }));
  },

  setProcessing: (processing) => {
    set({ isProcessing: processing });
  },

  clearCompleted: () => {
    set((state) => ({
      uploads: state.uploads.filter(
        (u) =>
          u.status === "pending" ||
          u.status === "uploading" ||
          u.status === "processing" ||
          u.status === "indexing"
      ),
    }));
  },

  retryUpload: (id) => {
    set((state) => ({
      uploads: state.uploads.map((u) =>
        u.id === id
          ? {
              ...u,
              status: "pending",
              uploadProgress: 0,
              progress: 0,
              processingProgress: null,
              wikiProgress: null,
              error: undefined,
              phase: null,
              phaseLabel: null,
              phaseMessage: null,
              processedUnits: null,
              totalUnits: null,
              unitLabel: null,
              elapsedSeconds: null,
              pollingStopped: false,
              longRunning: false,
            }
          : u
      ),
    }));

    const { isProcessing } = get();
    if (!isProcessing) {
      get().processQueue();
    }
  },

  stopPolling: (id) => {
    set((state) => ({
      uploads: state.uploads.map((u) =>
        u.id === id ? { ...u, pollingStopped: true } : u
      ),
    }));
  },

  processQueue: async () => {
    let acquired = false;

    set((state) => {
      if (state.isProcessing) {
        return state;
      }
      acquired = true;
      return { ...state, isProcessing: true };
    });

    if (!acquired) {
      return;
    }

    try {
      while (true) {
        const { uploads, activeVaultId } = get();
        const pendingUpload = uploads.find((u) => u.status === "pending");

        if (!pendingUpload) {
          break;
        }

        const currentUpload = uploads.find((u) => u.id === pendingUpload.id);
        if (!currentUpload || currentUpload.status !== "pending") {
          continue;
        }

        try {
          get().setStatus(pendingUpload.id, "uploading");

          const uploadResult = await uploadDocument(
            pendingUpload.file,
            (progress) => {
              get().updateUploadProgress(pendingUpload.id, progress);
            },
            activeVaultId || undefined
          );

          // Network upload finished. Don't claim "indexed" — the backend
          // is now the source of truth for processing/wiki state.
          const docId = String(uploadResult.id);
          set((state) => ({
            uploads: state.uploads.map((u) =>
              u.id === pendingUpload.id
                ? {
                    ...u,
                    status: "processing",
                    documentId: docId,
                    uploadProgress: 100,
                    progress: 100,
                    phase: "queued",
                    phaseLabel: phaseLabelFor("queued"),
                    phaseMessage: "Queued for processing",
                    phaseStartedAt: Date.now(),
                  }
                : u
            ),
          }));

          // Poll. Adaptive interval, no hard timeout (4-hour absolute cap).
          const startedAt = Date.now();
          let lastPhase: string | null = "queued";
          let lastWikiStatus: string | null = null;
          let phaseChangedAt = startedAt;
          let pollFailures = 0;

          while (true) {
            const elapsedMs = Date.now() - startedAt;
            // Adaptive cadence: ramp 1.5s -> 3s -> 6s based on time spent in
            // the *current* phase (resets to 1.5s on every observed phase
            // transition). The earlier expression collapsed to a constant.
            const delayMs = pickPollDelay(Date.now() - phaseChangedAt);
            await new Promise((r) => setTimeout(r, delayMs));

            const fresh = get().uploads.find((u) => u.id === pendingUpload.id);
            if (!fresh) break; // removed
            if (fresh.pollingStopped) break;
            if (fresh.status === "cancelled") break;
            if (elapsedMs > MAX_POLL_DURATION_MS) {
              // Stop polling; do NOT mark error — backend may still be working.
              get().stopPolling(pendingUpload.id);
              break;
            }
            if (
              elapsedMs > LONG_RUNNING_THRESHOLD_MS &&
              !fresh.longRunning
            ) {
              set((state) => ({
                uploads: state.uploads.map((u) =>
                  u.id === pendingUpload.id ? { ...u, longRunning: true } : u
                ),
              }));
            }

            try {
              const snapshot = await getDocumentStatus(docId);
              pollFailures = 0;
              get().applyStatusSnapshot(pendingUpload.id, snapshot);

              if (snapshot.phase && snapshot.phase !== lastPhase) {
                lastPhase = snapshot.phase;
                phaseChangedAt = Date.now();
              }
              if (snapshot.wiki_status && snapshot.wiki_status !== lastWikiStatus) {
                lastWikiStatus = snapshot.wiki_status;
              }

              if (snapshot.status === "error") {
                toast.error(`Failed to index ${pendingUpload.file.name}`, {
                  description: snapshot.error_message ?? undefined,
                });
                break;
              }
              if (snapshot.status === "indexed") {
                const wikiTerminal =
                  snapshot.wiki_status == null ||
                  snapshot.wiki_status === "completed" ||
                  snapshot.wiki_status === "failed" ||
                  snapshot.wiki_status === "cancelled";
                if (wikiTerminal) {
                  toast.success(`${pendingUpload.file.name} indexed`);
                  break;
                }
                // Indexed but wiki still working — keep polling for wiki.
                continue;
              }
            } catch {
              // Transient: keep trying. Bail only after many consecutive failures.
              pollFailures += 1;
              if (pollFailures >= 30) {
                get().setStatus(
                  pendingUpload.id,
                  "error",
                  "Status polling failed repeatedly. Refresh to retry."
                );
                break;
              }
            }
          }
        } catch (err) {
          const errorMsg = normalizeUploadErrorMessage(err);
          get().setStatus(pendingUpload.id, "error", errorMsg);
          toast.error(`Failed to upload ${pendingUpload.file.name}: ${errorMsg}`);
        }
      }
    } finally {
      set({ isProcessing: false });

      const { uploads } = get();
      if (uploads.some((u) => u.status === "pending")) {
        get().processQueue();
      }
    }
  },
}));
