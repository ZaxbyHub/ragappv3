import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  FileText,
  Search,
  Trash2,
  ScanLine,
  Loader2,
  Trash,
  Tag as TagIcon,
  FolderInput,
} from "lucide-react";
import {
  scanDocuments,
  deleteDocument,
  deleteDocuments,
  deleteAllDocumentsInVault,
  downloadDocument,
  listTags,
  listFolders,
  createFolder,
  updateFolder,
  deleteFolder,
  type Tag,
  type Folder,
  type DocumentSortBy,
  type SortOrder,
} from "@/lib/api";
// #766: the keepalive helper and the sanitized logger are imported directly
// from the api core module — the page's test suites replace the "@/lib/api"
// barrel with a manual mock object that does not re-export new helpers.
import { deleteDocumentKeepalive, logApiFailure } from "@/lib/api/core";
import { captureAuthOwner, type AuthOwner } from "@/lib/api/auth-lifecycle";
import { useDebounce } from "@/hooks/useDebounce";
import { useUploadMonitoring } from "@/hooks/useUploadMonitoring";
import { useVaultStore } from "@/stores/useVaultStore";
import { useAuthStore } from "@/stores/useAuthStore";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { useUploadStore } from "@/stores/useUploadStore";
import { VaultSelector } from "@/components/vault/VaultSelector";
import { VaultGate } from "@/components/vault/VaultGate";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/shared/ErrorState";
import {
  FILENAME_COL_WIDTH_MIN,
  FILENAME_COL_WIDTH_MAX,
  clampFilenameColWidth,
} from "@/lib/resizeClamp";
import { useBulkSelection } from "@/components/documents/useBulkSelection";
import { useDocumentPolling } from "@/components/documents/useDocumentPolling";
import { DocumentStatsCards } from "@/components/documents/DocumentStatsCards";
import { UploadDropzone } from "@/components/documents/UploadDropzone";
import { UploadQueue } from "@/components/documents/UploadQueue";
import { RejectedFilesBanner } from "@/components/documents/RejectedFilesBanner";
import { DocumentsTableSkeleton } from "@/components/documents/DocumentsTableSkeleton";
import { DocumentTable } from "@/components/documents/DocumentTable";
import { DocumentCardsList } from "@/components/documents/DocumentCardsList";
import { TagFilter } from "@/components/documents/TagFilter";
import { BulkTagDialog } from "@/components/documents/BulkTagDialog";
import { FolderTree } from "@/components/documents/FolderTree";
import { MoveFolderDialog } from "@/components/documents/MoveFolderDialog";
import { MoveToFolderDialog } from "@/components/documents/MoveToFolderDialog";
import { ConfirmDialog, type ConfirmDialogState } from "@/components/documents/ConfirmDialog";
import {
  isUploadTooLarge,
  uploadSizeExceededMessage,
  MAX_UPLOAD_FILE_SIZE_MB,
} from "@/lib/uploadLimits";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";

const FILENAME_COL_WIDTH_KEY = "ragapp_doc_table_filename_col";
const FILENAME_COL_WIDTH_DEFAULT = 250;

export default function DocumentsPage() {
  const navigate = useNavigate();
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearchQuery, isSearching] = useDebounce(searchQuery, 300);
  const [isScanning, setIsScanning] = useState(false);
  const [rejectedFiles, setRejectedFiles] = useState<string[]>([]);
  const [isBulkDeleting, setIsBulkDeleting] = useState(false);
  const [isBulkDeletingAll, setIsBulkDeletingAll] = useState(false);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>({
    open: false,
    title: "",
    description: "",
    onConfirm: () => {},
  });

  // Sorting + tag filtering
  const [sortBy, setSortBy] = useState<DocumentSortBy>("created_at");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");
  const [tagFilterId, setTagFilterId] = useState<number | null>(null);
  const [tags, setTags] = useState<Tag[]>([]);
  const [bulkTagOpen, setBulkTagOpen] = useState(false);

  // Folder hierarchy state
  const [folderFilterId, setFolderFilterId] = useState<number | null>(null);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [moveFolderOpen, setMoveFolderOpen] = useState(false);
  const [movingFolder, setMovingFolder] = useState<Folder | null>(null);

  // Persist the resizable filename column width across reloads.
  const [filenameColWidth, setFilenameColWidth] = useState<number>(() => {
    if (typeof window === "undefined") return FILENAME_COL_WIDTH_DEFAULT;
    try {
      const stored = window.localStorage.getItem(FILENAME_COL_WIDTH_KEY);
      const parsed = stored ? parseInt(stored, 10) : NaN;
      if (
        Number.isFinite(parsed) &&
        parsed >= FILENAME_COL_WIDTH_MIN &&
        parsed <= FILENAME_COL_WIDTH_MAX
      ) {
        return parsed;
      }
    } catch {
      // ignore
    }
    return FILENAME_COL_WIDTH_DEFAULT;
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(FILENAME_COL_WIDTH_KEY, String(filenameColWidth));
    } catch {
      // ignore (quota / private mode)
    }
  }, [filenameColWidth]);
  type ResizeCleanup = () => void;
  const activeResizeCleanupRef = useRef<ResizeCleanup | null>(null);

  const beginResizeGesture = useCallback(
    (mode: "mouse" | "touch", startX: number, startWidth: number) => {
      activeResizeCleanupRef.current?.();

      const originalCursor = document.body.style.cursor;
      const originalUserSelect = document.body.style.userSelect;
      let active = true;

      const applyClientX = (clientX: number) => {
        if (!active || activeResizeCleanupRef.current !== cleanup) return;
        setFilenameColWidth(clampFilenameColWidth(startWidth + clientX - startX));
      };
      const handleMouseMove = (moveEvent: MouseEvent) => {
        applyClientX(moveEvent.clientX);
      };
      const handleMouseUp = () => cleanup();
      const handleTouchMove = (moveEvent: TouchEvent) => {
        if (moveEvent.touches.length !== 1) {
          cleanup();
          return;
        }
        moveEvent.preventDefault();
        applyClientX(moveEvent.touches[0].clientX);
      };
      const handleTouchEnd = () => cleanup();
      const handleTouchCancel = () => cleanup();
      const handleBlur = () => cleanup();

      function cleanup() {
        if (!active) return;
        active = false;
        const ownsLifecycle = activeResizeCleanupRef.current === cleanup;

        if (mode === "mouse") {
          document.removeEventListener("mousemove", handleMouseMove);
          document.removeEventListener("mouseup", handleMouseUp);
        } else {
          document.removeEventListener("touchmove", handleTouchMove);
          document.removeEventListener("touchend", handleTouchEnd);
          document.removeEventListener("touchcancel", handleTouchCancel);
        }
        window.removeEventListener("blur", handleBlur);

        if (ownsLifecycle) {
          document.body.style.cursor = originalCursor;
          document.body.style.userSelect = originalUserSelect;
          activeResizeCleanupRef.current = null;
        }
      }

      activeResizeCleanupRef.current = cleanup;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      window.addEventListener("blur", handleBlur);
      if (mode === "mouse") {
        document.addEventListener("mousemove", handleMouseMove);
        document.addEventListener("mouseup", handleMouseUp);
      } else {
        document.addEventListener("touchmove", handleTouchMove, { passive: false });
        document.addEventListener("touchend", handleTouchEnd);
        document.addEventListener("touchcancel", handleTouchCancel);
      }
    },
    []
  );

  // Optimistic delete state
  const [optimisticallyDeletedIds, setOptimisticallyDeletedIds] = useState<Set<string>>(new Set());
  const pendingDeleteTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  // Owner captured when the delete was confirmed (review round 1 PRR-015):
  // a pending delete confirmed by one principal must never be dispatched
  // under another principal's bearer after an identity switch.
  const pendingDeleteOwnersRef = useRef<Map<string, AuthOwner>>(new Map());
  // Doc ids whose pagehide keepalive DELETE was dispatched but whose outcome
  // is unconfirmed (review round 1 PRR-006/PRR-008): a bfcache restore
  // replays them through the normal path so an Undo click can never re-show
  // a document whose delete already survived teardown.
  const dispatchedPagehideDeletesRef = useRef<Set<string>>(new Set());

  useEffect(
    () => () => {
      activeResizeCleanupRef.current?.();
    },
    []
  );

  // Flush pending deletes (#766 AC1/AC2, hardened per review round 1). The
  // user already confirmed each delete (the 3s undo window simply hadn't
  // elapsed); cancelling the timers would silently discard a confirmed
  // destructive action. The SPA-unmount flush and a pagehide flush share one
  // exactly-once helper: each timer entry is removed from the map BEFORE its
  // channel fires, so pagehide followed by unmount (or logout) can never
  // double-delete. Channel rules:
  // - pagehide: the keepalive channel when a CSRF credential is cached;
  //   when none is cached, NO request is attempted — the axios fallback
  //   would need a CSRF-discovery round trip the browser aborts during
  //   teardown (PRR-028), so the entry is dropped to the replay registry
  //   and, if the page returns (bfcache), replayed on pageshow.
  // - unmount (SPA navigation / logout): the normal deleteDocument path,
  //   whose failures now surface as a toast (AC13); a delete confirmed by a
  //   DIFFERENT principal than the current one is skipped and its row
  //   un-hidden — a principal never executes another principal's intent
  //   (PRR-015), and a 403 would lie about the outcome anyway.
  const flushPendingDeletes = useCallback((pagehide: boolean) => {
    const timers = pendingDeleteTimersRef.current;
    let failed = 0;
    const inflight: Promise<void>[] = [];
    const ownerNow = captureAuthOwner();
    timers.forEach((timer, docId) => {
      timers.delete(docId);
      clearTimeout(timer);
      const confirmedOwner = pendingDeleteOwnersRef.current.get(docId);
      pendingDeleteOwnersRef.current.delete(docId);
      if (confirmedOwner && ownerNow !== confirmedOwner) {
        // The confirming principal is gone; the intent dies with it.
        setOptimisticallyDeletedIds((previous) => {
          const next = new Set(previous);
          next.delete(docId);
          return next;
        });
        return;
      }
      if (pagehide) {
        dispatchedPagehideDeletesRef.current.add(docId);
        if (deleteDocumentKeepalive(docId)) return;
        // No CSRF credential: the keepalive channel never fired. Leave the
        // id in the replay registry — pageshow (bfcache return) replays it.
        return;
      }
      inflight.push(
        deleteDocument(docId).catch(() => {
          failed += 1;
        })
      );
    });
    if (inflight.length > 0) {
      // Counted aggregate toast AFTER settlement (review round 1
      // L1-05/M5-01; round-2 fix: the counter increments inside the async
      // catches, so the toast must wait for every delete to settle or it
      // can never fire — caught by frozen C13).
      void Promise.allSettled(inflight).then(() => {
        if (failed > 0) {
          toast.error(
            failed === 1
              ? "Failed to delete document"
              : `Failed to delete ${failed} documents`,
            { id: "delete-flush-failure" }
          );
        }
      });
    }
  }, []);

  useEffect(() => {
    const handlePageHide = () => flushPendingDeletes(true);
    // Bfcache restore (review round 1 PRR-006/PRR-008): the page is alive
    // again — replay every pagehide delete whose keepalive dispatch was not
    // confirmed (unconfirmed outcomes AND no-credential deferrals) through
    // the normal path, so the confirmed intent completes. The backend DELETE
    // is not idempotent (404 on a missing row), so a replay 404 means the
    // keepalive delete ALREADY landed — treated as completion, not failure
    // (review round 2). The registry is deliberately NOT cleared here: its
    // entries keep the Undo guard ("Delete already sent") truthful for the
    // rest of the page's life, including while a replay is in flight.
    const handlePageShow = (event: PageTransitionEvent) => {
      if (!event.persisted) return;
      const replay = Array.from(dispatchedPagehideDeletesRef.current);
      replay.forEach((docId) => {
        void deleteDocument(docId)
          .then(() => {
            toast.success("Deleted document");
          })
          .catch((err: unknown) => {
            const status =
              (err as { status?: number } | null)?.status ??
              (err as { response?: { status?: number } } | null)?.response?.status;
            if (status === 404) {
              // Already deleted by the keepalive dispatch.
              toast.success("Deleted document");
              return;
            }
            toast.error("Failed to delete document", { id: "delete-flush-failure" });
          });
      });
    };
    window.addEventListener("pagehide", handlePageHide);
    window.addEventListener("pageshow", handlePageShow);
    return () => {
      window.removeEventListener("pagehide", handlePageHide);
      window.removeEventListener("pageshow", handlePageShow);
      flushPendingDeletes(false);
    };
  }, [flushPendingDeletes]);

  // Before-logout flush (review round 1 PRR-007; round 2 channel fix): the
  // auth store dispatches this event FIRST, while the bearer and CSRF
  // credential are still valid — so this is SPA-unmount semantics (the
  // normal deleteDocument path, which can complete or toast), NOT pagehide
  // semantics. The pagehide-style keepalive/defer would drop the intent:
  // a no-credential deferral writes the replay registry, but an SPA logout
  // never fires pageshow, so nothing would ever replay it.
  useEffect(() => {
    const handleBeforeLogout = () => flushPendingDeletes(false);
    window.addEventListener("ragapp:before-logout", handleBeforeLogout);
    return () => window.removeEventListener("ragapp:before-logout", handleBeforeLogout);
  }, [flushPendingDeletes]);

  const { uploads, addUploads, cancelUpload, removeUpload, clearCompleted, retryUpload } =
    useUploadStore();
  // Monitor upload/wiki status for uploads initiated from this page even
  // when the user is not in chat (batched, attempt-ordered, auto-stops when
  // nothing is in flight).
  useUploadMonitoring();
  const { vaults, activeVaultId } = useVaultStore();
  const settings = useSettingsStore((state) => state.settings);
  const activeVault = vaults.find((vault) => vault.id === activeVaultId);
  const activeVaultPermission = activeVault?.current_user_permission ?? null;
  const canWriteActiveVault =
    activeVaultPermission === "write" || activeVaultPermission === "admin";
  const canAdminActiveVault = activeVaultPermission === "admin";
  const hasSelectedVault = activeVaultId != null && activeVault != null;
  // Destructive capability (vault ADMIN): mirrors the per-file vault-admin
  // delete routes (documents.py delete/batch/delete-all). Issue #767.
  const canMutateDocuments = hasSelectedVault && canAdminActiveVault;
  // Organise capability (vault WRITE): folders, bulk tag/move, row selection,
  // and wiki compile are all vault-write server-side (folders.py, tags.py,
  // wiki.py) — the UI must require no more and no less. Issue #767.
  const canOrganizeDocuments = hasSelectedVault && canWriteActiveVault;
  // Scan Directory is a GLOBAL-admin route (documents.py -> require_admin_role
  // accepts only global admin/superadmin); the client gate mirrors the user's
  // global role, not vault permission. Issue #767 AC5.
  const globalRole = useAuthStore((s) => s.user?.role);
  const canScanDirectories = globalRole === "admin" || globalRole === "superadmin";

  const {
    documents,
    setDocuments,
    stats,
    setStats,
    loading,
    listError,
    fetchDocuments,
    fetchStats,
    wikiStatusMap,
    wikiFetchFailedIds,
    compilingDocIds,
    handleCompileDocument,
    total,
    hasMore,
    loadMore,
  } = useDocumentPolling({
    activeVaultId,
    search: debouncedSearchQuery,
    sortBy,
    sortOrder,
    tagFilterId,
    folderFilterId,
    uploads,
  });

  const { selectedIds, setSelectedIds, clear: clearSelection, selectMany, selectOne } =
    useBulkSelection(canOrganizeDocuments);

  // Live mirror of the active vault for still-current checks inside async
  // continuations (fetch commits, dialog confirms): a ref read at commit time
  // sees switches the closure that started the work did not.
  const activeVaultIdRef = useRef<number | null>(activeVaultId);
  activeVaultIdRef.current = activeVaultId;
  // The vault the currently open confirm dialog was opened for; written by
  // every setConfirmDialog call site (all four) and READ by the two bulk
  // executors' execute-time guards — the folder and single-document confirms
  // are per-resource actions backed by per-vault server authorization and are
  // deliberately unguarded (recorded residual).
  const confirmVaultRef = useRef<number | null>(null);
  // Vault-context GENERATION for confirmations (complements confirmVaultRef):
  // a monotone counter bumped on every vault transition, so a round trip
  // (vault 1 -> 2 -> 1) — which the numeric-id compare cannot see — still
  // invalidates a dialog opened in the earlier incarnation. The numeric id
  // and the generation cover each other's windows: the id read is live from
  // render (catches a switch before the bump effect runs); the generation is
  // bumped in the switch effect (catches round trips back to the same id).
  const vaultSwitchGenRef = useRef(0);
  const confirmVaultGenRef = useRef(0);

  useEffect(() => {
    if (!canOrganizeDocuments) {
      setSelectedIds(new Set());
    }
  }, [canOrganizeDocuments, setSelectedIds]);

  // Bulk selection is per-vault display state: an admin->admin vault switch
  // never flips canMutateDocuments, so the permission-keyed effect above never
  // fires for it — clear the selection on the switch itself (issue #764 AC1;
  // the confirm-dialog path is covered by the execute-time guards instead).
  // Every transition also bumps the confirmation generation, so dialogs
  // opened before any later transition (including ABA round trips) refuse at
  // execute time.
  useEffect(() => {
    setSelectedIds(new Set());
    vaultSwitchGenRef.current += 1;
  }, [activeVaultId, setSelectedIds]);

  // Load the vault's tags for the filter + bulk-assign dialog.
  const fetchTags = useCallback(async () => {
    if (activeVaultId == null) {
      setTags([]);
      return;
    }
    // Issue #764 AC13: bind the commit to the ISSUED vault id (the argument
    // this call fetches for), not a live read — a stale-closure re-invocation
    // issues the old vault's request and must not commit over the current
    // vault's state when it resolves late.
    const issuedVaultId = activeVaultId;
    try {
      const next = await listTags(issuedVaultId);
      if (activeVaultIdRef.current !== issuedVaultId) return;
      setTags(next);
    } catch (err) {
      logApiFailure("Failed to load tags:", err);
    }
  }, [activeVaultId]);

  useEffect(() => {
    fetchTags();
  }, [fetchTags]);

  // Reset the tag filter when switching vaults (tag ids are vault-scoped),
  // and drop the previous vault's tag vocabulary immediately so a failed or
  // slow refetch can never render it (issue #764 AC4/AC5/AC13).
  useEffect(() => {
    setTagFilterId(null);
    setTags([]);
  }, [activeVaultId]);

  // Load the vault's folders for the sidebar tree + move dialog. Returns the
  // refreshed list so callers can reconcile against it (e.g. the folder filter
  // after a subtree delete).
  const fetchFolders = useCallback(async (): Promise<Folder[]> => {
    if (activeVaultId == null) {
      setFolders([]);
      return [];
    }
    // Same issued-vault guard as fetchTags (issue #764 AC13).
    const issuedVaultId = activeVaultId;
    try {
      const refreshed = await listFolders(issuedVaultId);
      if (activeVaultIdRef.current !== issuedVaultId) return [];
      setFolders(refreshed);
      return refreshed;
    } catch (err) {
      logApiFailure("Failed to load folders:", err);
      return [];
    }
  }, [activeVaultId]);

  useEffect(() => {
    fetchFolders();
  }, [fetchFolders]);

  // Reset the folder filter when switching vaults (folder ids are vault-
  // scoped), and drop the previous vault's folders immediately (issue #764
  // AC4/AC5/AC13).
  useEffect(() => {
    setFolderFilterId(null);
    setFolders([]);
  }, [activeVaultId]);

  const handleCreateFolder = useCallback(
    async (name: string, parentFolderId: number | null) => {
      if (activeVaultId == null) return;
      try {
        await createFolder(activeVaultId, name, parentFolderId);
        await fetchFolders();
        toast.success(`Created folder "${name}"`);
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Failed to create folder");
        throw err;
      }
    },
    [activeVaultId, fetchFolders]
  );

  const handleRenameFolder = useCallback(
    async (folderId: number, name: string) => {
      try {
        await updateFolder(folderId, { name });
        await fetchFolders();
        toast.success("Folder renamed");
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Failed to rename folder");
        throw err;
      }
    },
    [fetchFolders]
  );

  const handleDeleteFolder = useCallback(
    (folder: Folder) => {
      confirmVaultRef.current = activeVaultId;
      confirmVaultGenRef.current = vaultSwitchGenRef.current;
      setConfirmDialog({
        open: true,
        title: "Delete Folder",
        description: `Delete folder "${folder.name}"? Subfolders are also deleted; documents inside are moved out of the folder (not deleted).`,
        variant: "destructive",
        onConfirm: async () => {
          try {
            await deleteFolder(folder.id);
            // Reconcile the folder filter against the refreshed set — deleting
            // a subtree removes descendants too, so a filter pointing at any
            // folder that no longer exists (not just the deleted root) must
            // fall back to all documents. Clearing the filter retriggers the
            // query-driven document load without it.
            const refreshed = await fetchFolders();
            const filterRemoved =
              folderFilterId != null && !refreshed.some((f) => f.id === folderFilterId);
            if (filterRemoved) {
              setFolderFilterId(null);
            } else {
              await fetchDocuments();
            }
            toast.success("Folder deleted");
          } catch (err) {
            toast.error(err instanceof Error ? err.message : "Failed to delete folder");
          }
        },
      });
    },
    [folderFilterId, fetchFolders, fetchDocuments, activeVaultId]
  );

  const handleMoveFolderTrigger = useCallback((folder: Folder) => {
    setMovingFolder(folder);
  }, []);

  const handleSort = useCallback((column: DocumentSortBy) => {
    setSortBy((prevCol) => {
      if (prevCol === column) {
        setSortOrder((prev) => (prev === "asc" ? "desc" : "asc"));
        return prevCol;
      }
      setSortOrder(column === "created_at" ? "desc" : "asc");
      return column;
    });
  }, []);

  const handleResizeMouseDown = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      e.preventDefault();
      beginResizeGesture("mouse", e.clientX, filenameColWidth);
    },
    [beginResizeGesture, filenameColWidth]
  );

  // Keyboard parity for the filename-column resize handle (WCAG 2.1.1).
  // Drag-consistent direction: ArrowRight grows, ArrowLeft shrinks (matches
  // the mouse handler's deltaX = clientX - startX). Step = 16px grid unit.
  // Clamp via the shared helper so aria-valuenow stays within
  // [FILENAME_COL_WIDTH_MIN, FILENAME_COL_WIDTH_MAX] (matches the mouse +
  // touch handlers below).
  const handleResizeKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const KEYBOARD_RESIZE_STEP = 16;
      if (e.key === "ArrowRight") {
        e.preventDefault();
        setFilenameColWidth(
          clampFilenameColWidth(filenameColWidth + KEYBOARD_RESIZE_STEP)
        );
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        setFilenameColWidth(
          clampFilenameColWidth(filenameColWidth - KEYBOARD_RESIZE_STEP)
        );
      }
    },
    [filenameColWidth]
  );

  // Touch parity for the filename-column resize handle (WCAG 2.5.1).
  // Document touchmove listener attached with { passive: false } so
  // preventDefault suppresses page scroll during the drag.
  const handleResizeTouchStart = useCallback(
    (e: React.TouchEvent<HTMLDivElement>) => {
      if (e.touches.length !== 1) {
        activeResizeCleanupRef.current?.();
        return;
      }
      beginResizeGesture("touch", e.touches[0].clientX, filenameColWidth);
    },
    [beginResizeGesture, filenameColWidth]
  );

  const executeBulkDelete = useCallback(async () => {
    if (!canMutateDocuments) {
      toast.error("Select a vault you administer before deleting documents");
      return;
    }
    // Execute-time still-current guard (issue #764 AC1/AC14): the dialog
    // captured this closure with the selection from the vault it was opened
    // for; if the active vault changed while the modal was open (e.g. a
    // background fetchVaults correction), the captured ids belong to another
    // vault — refuse instead of sending them. A live-ref read, not a closed
    // value, sees the switch.
    if (
      confirmVaultRef.current !== activeVaultIdRef.current ||
      confirmVaultGenRef.current !== vaultSwitchGenRef.current
    ) {
      toast.error("Selection changed — select documents again");
      clearSelection();
      return;
    }
    setIsBulkDeleting(true);
    try {
      const result = await deleteDocuments(Array.from(selectedIds));
      // Reconcile from the response: only confirmed-deleted rows go away;
      // failed_ids rows must remain in the list.
      const failedIds = new Set((result.failed_ids ?? []).map((id) => String(id)));
      const deletedIds = new Set(
        Array.from(selectedIds).filter((id) => !failedIds.has(String(id)))
      );
      if (result.deleted_count > 0) {
        toast.success(`Deleted ${result.deleted_count} document${result.deleted_count > 1 ? "s" : ""}`);
      }
      if (failedIds.size > 0) {
        toast.error(`Failed to delete ${failedIds.size} document${failedIds.size > 1 ? "s" : ""}`);
      }
      clearSelection();
      // Refetch the authoritative list, then enforce the confirmed deletions
      // against the response (guards a racing/stale refetch re-adding rows the
      // server just deleted) while retaining the failed rows either way.
      await fetchDocuments();
      setDocuments((prev) => prev.filter((doc) => !deletedIds.has(String(doc.id))));
      // Optimistic single-field patch FIRST for the immediate render, then
      // refetch the authoritative stats LAST so the final committed value is
      // the server's post-delete state — ordering matters: patching after the
      // refetch would double-subtract deleted_count from the fresh server
      // total (implementation-review Round 1; issue #768 AC2).
      setStats((prev) =>
        prev
          ? {
              ...prev,
              total_documents: Math.max(0, (prev.total_documents ?? 0) - result.deleted_count),
            }
          : prev
      );
      await fetchStats();
      fetchFolders();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete documents");
    } finally {
      setIsBulkDeleting(false);
    }
  }, [
    canMutateDocuments,
    selectedIds,
    setDocuments,
    setStats,
    clearSelection,
    fetchDocuments,
    fetchStats,
    fetchFolders,
  ]);

  const handleBulkDelete = useCallback(() => {
    if (selectedIds.size === 0) return;
    confirmVaultRef.current = activeVaultId;
    confirmVaultGenRef.current = vaultSwitchGenRef.current;
    setConfirmDialog({
      open: true,
      title: "Delete Selected Documents",
      description: `Are you sure you want to delete ${selectedIds.size} document${selectedIds.size > 1 ? "s" : ""}? This action cannot be undone.`,
      onConfirm: executeBulkDelete,
      variant: "destructive",
    });
  }, [selectedIds, executeBulkDelete, activeVaultId]);

  const executeDeleteAllInVault = useCallback(async () => {
    if (!activeVaultId || !canMutateDocuments) return;
    // Same execute-time guard as executeBulkDelete (per the bulk-mutation
    // scope decision): the dialog captured the open-time activeVaultId in
    // this closure, so a stale confirm would wipe the PREVIOUS vault.
    // Delete-all is a bulk mutation.
    if (
      confirmVaultRef.current !== activeVaultIdRef.current ||
      confirmVaultGenRef.current !== vaultSwitchGenRef.current
    ) {
      toast.error("Vault changed — reopen the confirmation and try again");
      return;
    }
    setIsBulkDeletingAll(true);
    try {
      const result = await deleteAllDocumentsInVault(activeVaultId);
      if (result.deleted_count > 0) {
        toast.success(`Deleted ${result.deleted_count} document${result.deleted_count > 1 ? "s" : ""}`);
        setDocuments([]);
        setStats((prev) => (prev ? { ...prev, total_documents: 0 } : prev));
        fetchFolders();
        // Refetch the authoritative list: the optimistic empty above does not
        // touch the polling hook's `total`, which would otherwise leave
        // "Showing 0 of N" and a Load-more control pointing at rows that no
        // longer exist.
        await fetchDocuments();
        // Refetch stats for the same reason (issue #768 AC3): the optimistic
        // patch above only zeroes total_documents — Chunks/Size/Indexed must
        // agree with the emptied vault too.
        await fetchStats();
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete documents");
    } finally {
      setIsBulkDeletingAll(false);
    }
  }, [activeVaultId, canMutateDocuments, setDocuments, setStats, fetchFolders, fetchDocuments, fetchStats]);

  const handleDeleteAllInVault = useCallback(() => {
    if (!documents || documents.length === 0) return;
    if (!canMutateDocuments) {
      toast.error("Select a vault you administer before deleting documents");
      return;
    }
    confirmVaultRef.current = activeVaultId;
    confirmVaultGenRef.current = vaultSwitchGenRef.current;
    setConfirmDialog({
      open: true,
      title: "Delete All Documents in Vault",
      description: "Are you sure you want to delete ALL documents in this vault? This action cannot be undone.",
      onConfirm: executeDeleteAllInVault,
      variant: "destructive",
    });
  }, [documents, canMutateDocuments, executeDeleteAllInVault, activeVaultId]);

  // Effective client-side upload limit: the server-configured
  // max_file_size_mb from the settings store is the source of truth; the
  // local constant is only the fallback until settings have loaded.
  const maxFileSizeMb = settings?.max_file_size_mb;
  const effectiveMaxFileSizeMb =
    typeof maxFileSizeMb === "number" && maxFileSizeMb > 0
      ? maxFileSizeMb
      : MAX_UPLOAD_FILE_SIZE_MB;
  const effectiveMaxFileSizeBytes = effectiveMaxFileSizeMb * 1024 * 1024;

  const handleFiles = useCallback(
    (acceptedFiles: File[]) => {
      if (!hasSelectedVault || !canWriteActiveVault) {
        toast.error("Select a vault with write access before uploading");
        return;
      }
      // NOTE: pass the file explicitly — Array.prototype.filter also forwards
      // the index, which would otherwise become the limit argument (0 bytes)
      // and reject every file.
      const oversizedFiles = acceptedFiles.filter((file) =>
        isUploadTooLarge(file, effectiveMaxFileSizeBytes)
      );
      if (oversizedFiles.length > 0) {
        const rejected = oversizedFiles.map((file) =>
          uploadSizeExceededMessage(file.name, effectiveMaxFileSizeMb)
        );
        setRejectedFiles(rejected);
        rejected.forEach((message) => toast.error(`File rejected: ${message}`));
      } else {
        setRejectedFiles([]);
      }
      const queueableFiles = acceptedFiles.filter(
        (file) => !isUploadTooLarge(file, effectiveMaxFileSizeBytes)
      );
      if (queueableFiles.length === 0) return;
      addUploads(queueableFiles, activeVaultId ?? undefined);
      toast.success(`Added ${queueableFiles.length} file(s) to upload queue`);
    },
    [
      addUploads,
      activeVaultId,
      canWriteActiveVault,
      hasSelectedVault,
      effectiveMaxFileSizeBytes,
      effectiveMaxFileSizeMb,
    ]
  );

  const handleRejected = useCallback((names: string[]) => {
    setRejectedFiles(names);
    names.forEach((name) => toast.error(`File rejected: ${name}`));
  }, []);

  const handleScan = async () => {
    // Mirrors the route: POST /documents/scan depends on the GLOBAL admin
    // role and scans every vault's upload directory (issue #767 AC5/AC8) —
    // vault-write never granted scan rights.
    if (!canScanDirectories) {
      toast.error("A global admin role is required to scan directories");
      return;
    }
    setIsScanning(true);
    try {
      const result = await scanDocuments();
      toast.success(`Scan complete: ${result.added} new document(s) added, ${result.scanned} scanned`);
      await Promise.all([fetchDocuments(), fetchStats()]);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Scan failed");
    } finally {
      setIsScanning(false);
    }
  };

  const handleDeleteDocument = (docId: string, filename?: string) => {
    if (!canMutateDocuments) {
      toast.error("Select a vault you administer before deleting documents");
      return;
    }
    confirmVaultRef.current = activeVaultId;
    confirmVaultGenRef.current = vaultSwitchGenRef.current;
    const target = filename ?? `document ${docId}`;
    setConfirmDialog({
      open: true,
      title: "Delete Document",
      // Name the target (issue #768 AC5): the dialog is the only user-facing
      // identification of what is about to be destroyed.
      description: `Are you sure you want to delete "${target}"? This will also remove all associated chunks.`,
      confirmLabel: "Delete",
      onConfirm: () => {
        if (pendingDeleteTimersRef.current.has(docId)) return;
        // Owner at confirm time (review round 1 PRR-015): the flush never
        // dispatches another principal's confirmed intent.
        pendingDeleteOwnersRef.current.set(docId, captureAuthOwner());

        setOptimisticallyDeletedIds((prev) => new Set(prev).add(docId));

        const toastId = toast("Document deleted", {
          description: "You can undo this action",
          duration: 3000,
          action: {
            label: "Undo",
            onClick: () => {
              const timer = pendingDeleteTimersRef.current.get(docId);
              if (timer) {
                clearTimeout(timer);
                pendingDeleteTimersRef.current.delete(docId);
              }
              // If a teardown flush already dispatched this delete
              // (review round 1 PRR-008), Undo must not resurrect the
              // row: the destructive intent is already in flight and the
              // pageshow replay (bfcache) completes it.
              if (dispatchedPagehideDeletesRef.current.has(docId)) {
                toast.dismiss(toastId);
                toast.info("Delete already sent — the document will be removed");
                return;
              }
              setOptimisticallyDeletedIds((prev) => {
                const next = new Set(prev);
                next.delete(docId);
                return next;
              });
              toast.dismiss(toastId);
            },
          },
        });

        const timer = setTimeout(async () => {
          pendingDeleteTimersRef.current.delete(docId);
          pendingDeleteOwnersRef.current.delete(docId);
          toast.dismiss(toastId);
          try {
            await deleteDocument(docId);
            toast.success("Document deleted successfully");
            await Promise.all([fetchDocuments(), fetchStats(), fetchFolders()]);
            setOptimisticallyDeletedIds((prev) => {
              const next = new Set(prev);
              next.delete(docId);
              return next;
            });
          } catch (err) {
            toast.error(err instanceof Error ? err.message : "Failed to delete document");
            setOptimisticallyDeletedIds((prev) => {
              const next = new Set(prev);
              next.delete(docId);
              return next;
            });
            await fetchDocuments();
          }
        }, 3000);

        pendingDeleteTimersRef.current.set(docId, timer);
      },
      variant: "destructive",
    });
  };

  const handleDownloadDocument = async (doc: { id: string; filename?: string }) => {
    try {
      await downloadDocument(doc.id, doc.filename || `document-${doc.id}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to download document");
    }
  };

  // Server already filters by search/tag — only apply the local optimistic-delete mask.
  const filteredDocuments = useMemo(
    () => documents?.filter((doc) => !optimisticallyDeletedIds.has(String(doc.id))) ?? [],
    [documents, optimisticallyDeletedIds]
  );

  // Header select-all applies ONLY to the visible rows: checking adds the
  // visible ids (selections outside the current view are preserved) and
  // unchecking removes exactly the visible ids.
  const handleSelectAll = useCallback(
    (checked: boolean | "indeterminate") => {
      selectMany(
        filteredDocuments.map((doc) => String(doc.id)),
        checked === true
      );
    },
    [selectMany, filteredDocuments]
  );
  const hasActiveSearch = searchQuery.trim().length > 0;
  const hasStats = stats !== null;
  const totalVaultDocuments = stats?.total_documents ?? 0;
  // Issue #764 AC10: the resolving placeholder must also require that no rows
  // are visible — after a stats failure `!hasStats` stays true forever, and
  // without the row check the skeleton hides real search results permanently.
  const isResolvingSearchEmptyState =
    hasSelectedVault && hasActiveSearch && !hasStats && filteredDocuments.length === 0;
  const emptyState =
    vaults.length === 0
      ? {
          title: "No vaults available",
          description: "Create a vault or ask an admin to grant you access to start uploading documents.",
          action: { label: "Open Vaults", onClick: () => navigate("/vaults") },
        }
      : !hasSelectedVault
        ? {
            title: "Select a vault to view documents",
            description: "Documents are scoped to the active vault.",
            // Issue #781 (UI-R2-03): the no-selection state points at
            // vault scoping without offering a control from where the user
            // stands — VaultGate renders the selector (and the create/open
            // action) inline, unlike the copy-only branch it replaces.
            action: <VaultGate />,
          }
        : (hasActiveSearch || tagFilterId != null || folderFilterId != null) &&
            totalVaultDocuments > 0
          ? hasActiveSearch
            ? {
                title: "No documents match your search",
                description: "Search checks filename, type, status, source, sender, subject, and document date.",
                action: {
                  label: "Clear filters",
                  onClick: () => {
                    setTagFilterId(null);
                    setFolderFilterId(null);
                  },
                },
              }
            : {
                // A tag/folder filter with zero matches in a populated vault is
                // a filter miss, not an empty vault (issue #768 AC4).
                title: "No documents match your filters",
                description: "The selected tag or folder does not match any document in this vault.",
                action: {
                  label: "Clear filters",
                  onClick: () => {
                    setTagFilterId(null);
                    setFolderFilterId(null);
                  },
                },
              }
          : {
            title: "No documents yet",
            description: canWriteActiveVault
              ? "Upload files to get started."
              : "You have read-only access to this vault. Create a vault you administer or ask an admin for write access to upload documents.",
            action: canWriteActiveVault
              ? undefined
              : { label: "Open Vaults", onClick: () => navigate("/vaults") },
          };

  const selectedFileIds = useMemo(
    () => Array.from(selectedIds).map((id) => Number(id)),
    [selectedIds]
  );

  // Retry affordance for a failed list fetch (issue #258 / legacy-14): a
  // network blip or backend restart must be distinguishable from an empty
  // vault, and recoverable without a full page reload.
  const retryLoadDocuments = useCallback(() => {
    void fetchDocuments();
    void fetchStats();
  }, [fetchDocuments, fetchStats]);

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12">
      <PageTitleHeader
        title="Documents"
        description="Manage your knowledge base documents"
        actions={
          <>
            <VaultSelector />
            <Button
              onClick={handleScan}
              disabled={isScanning || !canScanDirectories}
              title={
                canScanDirectories
                  ? "Scan every vault's upload directory for new files"
                  : "A global admin role is required to scan directories"
              }
            >
              {isScanning ? (
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
              ) : (
                <ScanLine className="w-4 h-4 mr-2" />
              )}
              Scan Directory
            </Button>
            {filteredDocuments.length > 0 && canMutateDocuments && (
              <Button variant="destructive" onClick={handleDeleteAllInVault} disabled={isBulkDeletingAll}>
                {isBulkDeletingAll ? (
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                ) : (
                  <Trash className="w-4 h-4 mr-2" />
                )}
                Delete All in Vault
              </Button>
            )}
          </>
        }
      />

      {stats && <DocumentStatsCards stats={stats} />}

      <UploadDropzone
        hasSelectedVault={hasSelectedVault}
        canWriteActiveVault={canWriteActiveVault}
        hasActiveVaultId={activeVaultId != null}
        onFiles={handleFiles}
        onRejected={handleRejected}
      />

      <UploadQueue
        uploads={uploads}
        onClearCompleted={clearCompleted}
        onCancel={cancelUpload}
        onRetry={retryUpload}
        onRemove={removeUpload}
      />

      <RejectedFilesBanner files={rejectedFiles} onDismiss={() => setRejectedFiles([])} />

      <div className="flex items-center gap-4">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            placeholder="Search documents and metadata..."
            className="pl-10"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          {isSearching && (
            <Loader2 className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground animate-spin" />
          )}
        </div>
        <TagFilter tags={tags} value={tagFilterId} onChange={setTagFilterId} />
        <div className="flex items-center gap-2">
          {selectedIds.size > 0 && canOrganizeDocuments && (
            <>
              <Badge variant="outline">{selectedIds.size} selected</Badge>
              <Button variant="outline" size="sm" onClick={() => setBulkTagOpen(true)}>
                <TagIcon className="w-4 h-4 mr-2" />
                Tag
              </Button>
              <Button variant="outline" size="sm" onClick={() => setMoveFolderOpen(true)}>
                <FolderInput className="w-4 h-4 mr-2" />
                Move
              </Button>
              {canMutateDocuments && (
                <Button variant="destructive" size="sm" onClick={handleBulkDelete} disabled={isBulkDeleting}>
                {isBulkDeleting ? (
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                ) : (
                  <Trash2 className="w-4 h-4 mr-2" />
                )}
                  Delete Selected
                </Button>
              )}
            </>
          )}
          {/* Server total for the current filtered query — the badge must not
              understate the match set when only the first window is loaded
              (issue #768 AC1). The floor keeps the badge sane in the
              optimistic-delete window before the refetch lands. */}
          <Badge variant="secondary">{Math.max(total, filteredDocuments.length)} documents</Badge>
        </div>
      </div>

      {/* Issue #778 (WCAG 1.4.10 reflow): the folder tree stacks above the
          list below lg — a flat flex row squeezed the document list to ~8px
          at 320px. */}
      <div className="flex flex-col gap-6 lg:flex-row">
        {hasSelectedVault && (
          <FolderTree
            folders={folders}
            selectedFolderId={folderFilterId}
            onSelect={setFolderFilterId}
            canMutate={canOrganizeDocuments}
            onCreate={handleCreateFolder}
            onRename={handleRenameFolder}
            onDelete={handleDeleteFolder}
            onMove={handleMoveFolderTrigger}
          />
        )}
        <div className="flex-1 min-w-0">
      {loading ? (
        <DocumentsTableSkeleton />
      ) : listError && filteredDocuments.length === 0 ? (
        <ErrorState
          title="Failed to load documents"
          description="The document list could not be fetched. Check your connection and try again."
          action={{ label: "Try Again", onClick: retryLoadDocuments }}
        />
      ) : isResolvingSearchEmptyState ? (
        <div className="space-y-3" role="status" aria-live="polite">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-4 w-96 max-w-full" />
        </div>
      ) : filteredDocuments.length === 0 ? (
        <EmptyState
          icon={FileText}
          title={emptyState.title}
          description={emptyState.description}
          action={emptyState.action}
        />
      ) : (
        <>
          <DocumentTable
            documents={filteredDocuments}
            selectedIds={selectedIds}
            canOrganizeDocuments={canOrganizeDocuments}
            canMutateDocuments={canMutateDocuments}
            filenameColWidth={filenameColWidth}
            onResizeMouseDown={handleResizeMouseDown}
            onResizeKeyDown={handleResizeKeyDown}
            onResizeTouchStart={handleResizeTouchStart}
            onSelectAll={handleSelectAll}
            onSelectOne={selectOne}
            wikiStatusMap={wikiStatusMap}
            wikiFetchFailedIds={wikiFetchFailedIds}
            compilingDocIds={compilingDocIds}
            onCompileDocument={handleCompileDocument}
            onDownload={handleDownloadDocument}
            onDelete={handleDeleteDocument}
            sortBy={sortBy}
            sortOrder={sortOrder}
            onSort={handleSort}
          />
          <DocumentCardsList
            documents={filteredDocuments}
            canOrganizeDocuments={canOrganizeDocuments}
            canMutateDocuments={canMutateDocuments}
            selectedIds={selectedIds}
            onSelectOne={selectOne}
            onDelete={handleDeleteDocument}
            onDownload={handleDownloadDocument}
          />
          {total > filteredDocuments.length && (
            <div className="flex flex-col items-center gap-2 py-4">
              <p className="text-sm text-muted-foreground">
                Showing {filteredDocuments.length} of {total} documents
              </p>
              {hasMore && filteredDocuments.length < 1000 ? (
                <Button variant="outline" size="sm" onClick={loadMore}>
                  Load more
                </Button>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Use search to narrow down the remaining documents.
                </p>
              )}
            </div>
          )}
        </>
      )}
        </div>
      </div>

      {activeVaultId != null && (
        <BulkTagDialog
          open={bulkTagOpen}
          onOpenChange={setBulkTagOpen}
          vaultId={activeVaultId}
          selectedFileIds={selectedFileIds}
          tags={tags}
          onTagsChanged={fetchTags}
          onAssigned={() => {
            fetchTags();
            fetchDocuments();
          }}
        />
      )}

      {activeVaultId != null && (
        <MoveToFolderDialog
          open={moveFolderOpen}
          onOpenChange={setMoveFolderOpen}
          vaultId={activeVaultId}
          selectedFileIds={selectedFileIds}
          folders={folders}
          onMoved={() => {
            clearSelection();
            fetchFolders();
            fetchDocuments();
          }}
        />
      )}

      {movingFolder && (
        <MoveFolderDialog
          key={movingFolder.id}
          open={movingFolder != null}
          onOpenChange={(open) => {
            if (!open) setMovingFolder(null);
          }}
          folder={movingFolder}
          folders={folders}
          onMoved={() => {
            setMovingFolder(null);
            fetchFolders();
          }}
        />
      )}

      <ConfirmDialog
        state={confirmDialog}
        onOpenChange={(open) => setConfirmDialog((prev) => ({ ...prev, open }))}
      />
    </div>
  );
}
