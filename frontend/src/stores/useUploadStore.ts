import { create } from "zustand";
import { uploadDocument } from "@/lib/api";
import { useSettingsStore } from "@/stores/useSettingsStore";
import {
  MAX_UPLOAD_FILE_SIZE_MB,
  isUploadTooLarge,
  normalizeUploadErrorMessage,
  uploadSizeExceededMessage,
} from "@/lib/uploadLimits";
import { toast } from "sonner";

/**
 * UploadFile state machine
 * ------------------------
 *   pending -> uploading -> processing -> indexed | error | cancelled
 *
 * `pending`/`uploading` are byte-transfer states owned by the bounded
 * transfer pool (see `UPLOAD_CONCURRENCY`). Once the server accepts the
 * bytes the upload moves to `processing` with a `documentId` and monitoring
 * becomes the batched status poller's job (`hooks/useUploadMonitoring.ts`)
 * — the transfer pool never waits for indexing.
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

/**
 * Maximum number of concurrent byte-transfers. A pool slot is held ONLY for
 * the `uploadDocument` await: as soon as the server accepts a file's bytes
 * (the upload moves to `processing`) the slot frees and the next pending
 * transfer starts — transfers never serialize behind indexing, which is
 * monitored separately by the batched status poller.
 */
export const UPLOAD_CONCURRENCY = 3;

/**
 * Status snapshot shape accepted by `applyStatusSnapshot`. Both the per-file
 * `DocumentStatusResponse` and batched `DocumentStatusEntry` payloads are
 * structurally compatible.
 */
export interface UploadStatusSnapshot {
  id: string | number;
  status: string;
  filename?: string | null;
  chunk_count?: number | null;
  error_message?: string | null;
  phase?: string | null;
  phase_message?: string | null;
  progress_percent?: number | null;
  processed_units?: number | null;
  total_units?: number | null;
  unit_label?: string | null;
  elapsed_seconds?: number | null;
  wiki_status?: string | null;
}

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
  /** Backend chunk count once indexing succeeds (informational). */
  chunkCount?: number | null;
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
  /** True once a server status snapshot has been applied to this upload. */
  statusSeen?: boolean;
  /** True once monitoring has been stopped for this upload (manual stop or hard cap). */
  pollingStopped?: boolean;
  /** True once the long-running banner should be shown (>= 30 min processing). */
  longRunning?: boolean;
}

interface UploadState {
  uploads: UploadFile[];
  isProcessing: boolean;
  activeVaultId: number | null;
  /** Upload ids currently attached to the chat composer (survives unmounts). */
  chatAttachmentIds: string[];

  // Actions
  /** Queue files for upload; returns the ids of the uploads that were queued. */
  addUploads: (files: File[], vaultId?: number) => string[];
  cancelUpload: (id: string) => void;
  removeUpload: (id: string) => void;
  updateUploadProgress: (id: string, progress: number) => void;
  /** @deprecated alias of updateUploadProgress for legacy callers */
  updateProgress: (id: string, progress: number) => void;
  setStatus: (id: string, status: UploadStatus, error?: string) => void;
  /**
   * Apply a server status snapshot. `seq` is the monitoring attempt's
   * monotonically increasing sequence number: a snapshot from an older (or
   * equal) attempt is ignored, and terminal states are sticky — a
   * non-terminal snapshot never regresses an upload that already finished.
   */
  applyStatusSnapshot: (id: string, snapshot: UploadStatusSnapshot, seq?: number) => void;
  setProcessing: (processing: boolean) => void;
  clearCompleted: () => void;
  retryUpload: (id: string) => void;
  /** Stop monitoring this upload without cancelling backend processing. */
  stopPolling: (id: string) => void;
  /** Register an upload as a chat attachment (chips survive unmount/remount). */
  attachToChat: (id: string) => void;
  /** Remove an upload's chat attachment registration. */
  detachFromChat: (id: string) => void;
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

/** Terminal upload states — once reached, non-terminal snapshots cannot regress them. */
const TERMINAL_UPLOAD_STATUSES: ReadonlySet<UploadStatus> = new Set([
  "indexed",
  "error",
  "cancelled",
]);

function isTerminalUploadStatus(status: UploadStatus): boolean {
  return TERMINAL_UPLOAD_STATUSES.has(status);
}

/** Wiki states that still require monitoring once the document itself is indexed. */
function isTransientWikiStatus(wikiStatus?: string | null): boolean {
  return wikiStatus === "pending" || wikiStatus === "running";
}

/**
 * True while the batched status monitor should keep polling for this upload:
 * bytes are accepted (`documentId` assigned) and the document — or its wiki
 * compile — has not reached a terminal state.
 */
export function uploadNeedsMonitoring(upload: UploadFile): boolean {
  if (!upload.documentId || upload.pollingStopped) return false;
  if (upload.status === "indexed") return isTransientWikiStatus(upload.wikiStatus);
  return upload.status === "processing" || upload.status === "indexing";
}

// Monotonic snapshot-application ordering (issue #514 / UPLOAD-DEEP-01):
// the monitoring attempt's seq is recorded per upload so a late response
// from an older attempt can never overwrite a newer snapshot.
const lastAppliedSeqByUploadId = new Map<string, number>();

/**
 * Map a backend status snapshot onto our local UploadFile fields. We don't
 * touch fields the backend didn't supply so existing client state survives
 * partial responses.
 */
function snapshotToPatch(
  snapshot: UploadStatusSnapshot,
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
    chunkCount: snapshot.chunk_count ?? null,
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

/**
 * Effective client-side upload limit in MB: the server-configured
 * `max_file_size_mb` once settings have loaded, else the default fallback.
 */
export function effectiveUploadLimitMb(): number {
  return useSettingsStore.getState().settings?.max_file_size_mb ?? MAX_UPLOAD_FILE_SIZE_MB;
}

export const useUploadStore = create<UploadState>((set, get) => {
  /**
   * Run one byte-transfer. A pool slot corresponds to the `uploadDocument`
   * await only: when the server accepts the bytes the upload moves to
   * `processing` (monitoring takes over) and the slot frees for the next
   * pending file. Failures mark the upload error and free the slot.
   */
  const runTransfer = async (
    uploadId: string,
    file: File,
    vaultId: number | null
  ): Promise<void> => {
    try {
      const uploadResult = await uploadDocument(
        file,
        (progress) => {
          get().updateUploadProgress(uploadId, progress);
        },
        vaultId ?? undefined
      );

      // Network upload finished. Don't claim "indexed" — the backend
      // is now the source of truth for processing/wiki state.
      const docId = String(uploadResult.id);
      set((state) => ({
        uploads: state.uploads.map((u) =>
          u.id === uploadId
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
      toast.success(`${file.name} uploaded`);
    } catch (err) {
      const errorMsg = normalizeUploadErrorMessage(err);
      get().setStatus(uploadId, "error", errorMsg);
      toast.error(`Failed to upload ${file.name}: ${errorMsg}`);
    } finally {
      // Free the slot and immediately admit the next pending transfer.
      void get().processQueue();
    }
  };

  return {
    uploads: [],
    isProcessing: false,
    activeVaultId: null,
    chatAttachmentIds: [],

    addUploads: (files, vaultId) => {
      if (!vaultId) {
        toast.error("No vault selected. Please select a vault before uploading.");
        return [];
      }
      const limitMb = effectiveUploadLimitMb();
      const limitBytes = limitMb * 1024 * 1024;
      const acceptedFiles = files.filter((file) => {
        if (!isUploadTooLarge(file, limitBytes)) return true;
        toast.error(uploadSizeExceededMessage(file.name, limitMb));
        return false;
      });
      if (acceptedFiles.length === 0) return [];
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
        statusSeen: false,
        startedAt: now,
      }));

      set((state) => ({
        uploads: [...state.uploads, ...newUploads],
        activeVaultId: vaultId || state.activeVaultId,
      }));

      void get().processQueue();
      return newUploads.map((u) => u.id);
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
      // Evict the snapshot-ordering entry so the map tracks live uploads only.
      lastAppliedSeqByUploadId.delete(id);
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

    applyStatusSnapshot: (id, snapshot, seq) => {
      set((state) => ({
        uploads: state.uploads.map((u) => {
          if (u.id !== id) return u;
          // Attempt ordering: ignore snapshots from an older (or repeated)
          // monitoring attempt — its information is stale by construction.
          if (seq != null) {
            const lastSeq = lastAppliedSeqByUploadId.get(id);
            if (lastSeq != null && seq <= lastSeq) return u;
            lastAppliedSeqByUploadId.set(id, seq);
          }
          const patch = snapshotToPatch(snapshot, u.phase);
          // Terminal states are sticky: a non-terminal snapshot (late or
          // out-of-order) never regresses a finished upload.
          if (
            isTerminalUploadStatus(u.status) &&
            !isTerminalUploadStatus(patch.status ?? u.status)
          ) {
            return u;
          }
          return { ...u, ...patch, statusSeen: true };
        }),
      }));
    },

    setProcessing: (processing) => {
      set({ isProcessing: processing });
    },

    clearCompleted: () => {
      set((state) => {
        const kept = state.uploads.filter(
          (u) =>
            u.status === "pending" ||
            u.status === "uploading" ||
            u.status === "processing" ||
            u.status === "indexing"
        );
        // Evict snapshot-ordering entries for the dropped terminal rows.
        if (kept.length !== state.uploads.length) {
          const keptIds = new Set(kept.map((u) => u.id));
          for (const upload of state.uploads) {
            if (!keptIds.has(upload.id)) lastAppliedSeqByUploadId.delete(upload.id);
          }
        }
        return { uploads: kept };
      });
    },

    retryUpload: (id) => {
      // A retry restarts the lifecycle; drop the stale ordering entry so a
      // fresh monitoring sequence applies immediately.
      lastAppliedSeqByUploadId.delete(id);
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
                statusSeen: false,
                pollingStopped: false,
                longRunning: false,
              }
            : u
        ),
      }));

      void get().processQueue();
    },

    stopPolling: (id) => {
      set((state) => ({
        uploads: state.uploads.map((u) =>
          u.id === id ? { ...u, pollingStopped: true } : u
        ),
      }));
    },

    attachToChat: (id) => {
      set((state) =>
        state.chatAttachmentIds.includes(id)
          ? state
          : { chatAttachmentIds: [...state.chatAttachmentIds, id] }
      );
    },

    detachFromChat: (id) => {
      set((state) => ({
        chatAttachmentIds: state.chatAttachmentIds.filter(
          (attachmentId) => attachmentId !== id
        ),
      }));
    },

    processQueue: async () => {
      // Bounded transfer pool: count in-flight transfers from the store rows
      // (an "uploading" row is exactly one live `uploadDocument` await) and
      // keep starting pending transfers until the pool is full. No awaits in
      // this loop — re-entrant calls simply observe the already-marked rows.
      while (true) {
        const { uploads, activeVaultId } = get();
        const inFlight = uploads.filter((u) => u.status === "uploading").length;
        if (inFlight >= UPLOAD_CONCURRENCY) {
          set({ isProcessing: true });
          return;
        }
        const next = uploads.find((u) => u.status === "pending");
        if (!next) {
          set({ isProcessing: inFlight > 0 });
          return;
        }
        get().setStatus(next.id, "uploading");
        void runTransfer(next.id, next.file, activeVaultId);
      }
    },
  };
});
