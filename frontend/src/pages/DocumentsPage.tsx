import { useState, useEffect, useCallback, useMemo, useRef } from "react";
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
import { useDebounce } from "@/hooks/useDebounce";
import { useVaultStore } from "@/stores/useVaultStore";
import { useUploadStore } from "@/stores/useUploadStore";
import { VaultSelector } from "@/components/vault/VaultSelector";
import { EmptyState } from "@/components/shared/EmptyState";
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
import { isUploadTooLarge, uploadSizeExceededMessage } from "@/lib/uploadLimits";

const FILENAME_COL_WIDTH_KEY = "ragapp_doc_table_filename_col";
const FILENAME_COL_WIDTH_DEFAULT = 250;

export default function DocumentsPage() {
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
      if (Number.isFinite(parsed) && parsed >= 120 && parsed <= 600) return parsed;
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
  const dragState = useRef<{ startX: number; startWidth: number }>({ startX: 0, startWidth: 0 });

  // Optimistic delete state
  const [optimisticallyDeletedIds, setOptimisticallyDeletedIds] = useState<Set<string>>(new Set());
  const pendingDeleteTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  // Cleanup on unmount: restore cursor if destroyed mid-drag.
  useEffect(() => {
    return () => {
      document.body.style.cursor = "";
    };
  }, []);

  // Cleanup pending delete timers on unmount.
  useEffect(() => {
    return () => {
      // eslint-disable-next-line react-hooks/exhaustive-deps
      pendingDeleteTimersRef.current.forEach((timer) => clearTimeout(timer));
    };
  }, []);

  const { uploads, addUploads, cancelUpload, removeUpload, clearCompleted, retryUpload } =
    useUploadStore();
  const { vaults, activeVaultId } = useVaultStore();
  const activeVault = vaults.find((vault) => vault.id === activeVaultId);
  const activeVaultPermission = activeVault?.current_user_permission ?? null;
  const canWriteActiveVault =
    activeVaultPermission === "write" || activeVaultPermission === "admin";
  const canAdminActiveVault = activeVaultPermission === "admin";
  const hasSelectedVault = activeVaultId != null && activeVault != null;
  const canMutateDocuments = hasSelectedVault && canAdminActiveVault;

  const {
    documents,
    setDocuments,
    stats,
    setStats,
    loading,
    fetchDocuments,
    fetchStats,
    wikiStatusMap,
    compilingDocIds,
    handleCompileDocument,
  } = useDocumentPolling({
    activeVaultId,
    search: debouncedSearchQuery,
    sortBy,
    sortOrder,
    tagFilterId,
    folderFilterId,
    uploads,
  });

  const { selectedIds, setSelectedIds, clear: clearSelection, selectAll, selectOne } =
    useBulkSelection(canMutateDocuments);

  useEffect(() => {
    if (!canMutateDocuments) {
      setSelectedIds(new Set());
    }
  }, [canMutateDocuments, setSelectedIds]);

  // Load the vault's tags for the filter + bulk-assign dialog.
  const fetchTags = useCallback(async () => {
    if (activeVaultId == null) {
      setTags([]);
      return;
    }
    try {
      setTags(await listTags(activeVaultId));
    } catch (err) {
      console.error("Failed to load tags:", err);
    }
  }, [activeVaultId]);

  useEffect(() => {
    fetchTags();
  }, [fetchTags]);

  // Reset the tag filter when switching vaults (tag ids are vault-scoped).
  useEffect(() => {
    setTagFilterId(null);
  }, [activeVaultId]);

  // Load the vault's folders for the sidebar tree + move dialog.
  const fetchFolders = useCallback(async () => {
    if (activeVaultId == null) {
      setFolders([]);
      return;
    }
    try {
      setFolders(await listFolders(activeVaultId));
    } catch (err) {
      console.error("Failed to load folders:", err);
    }
  }, [activeVaultId]);

  useEffect(() => {
    fetchFolders();
  }, [fetchFolders]);

  // Reset the folder filter when switching vaults (folder ids are vault-scoped).
  useEffect(() => {
    setFolderFilterId(null);
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
      setConfirmDialog({
        open: true,
        title: "Delete Folder",
        description: `Delete folder "${folder.name}"? Subfolders are also deleted; documents inside are moved out of the folder (not deleted).`,
        variant: "destructive",
        onConfirm: async () => {
          try {
            await deleteFolder(folder.id);
            // If the active filter pointed at a removed folder, fall back to all.
            setFolderFilterId((prev) => (prev === folder.id ? null : prev));
            await Promise.all([fetchFolders(), fetchDocuments()]);
            toast.success("Folder deleted");
          } catch (err) {
            toast.error(err instanceof Error ? err.message : "Failed to delete folder");
          }
        },
      });
    },
    [fetchFolders, fetchDocuments]
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
      dragState.current = { startX: e.clientX, startWidth: filenameColWidth };
      const originalCursor = document.body.style.cursor;
      document.body.style.cursor = "col-resize";

      const handleMouseMove = (moveEvent: MouseEvent) => {
        const deltaX = moveEvent.clientX - dragState.current.startX;
        const newWidth = Math.max(120, Math.min(600, dragState.current.startWidth + deltaX));
        setFilenameColWidth(newWidth);
      };

      const handleMouseUp = () => {
        document.body.style.cursor = originalCursor;
        document.removeEventListener("mousemove", handleMouseMove);
        document.removeEventListener("mouseup", handleMouseUp);
      };

      document.addEventListener("mousemove", handleMouseMove);
      document.addEventListener("mouseup", handleMouseUp);
    },
    [filenameColWidth]
  );

  const handleSelectAll = useCallback(
    (checked: boolean | "indeterminate") => {
      if (checked) selectAll(documents?.map((doc) => String(doc.id)) ?? []);
      else clearSelection();
    },
    [selectAll, clearSelection, documents]
  );

  const executeBulkDelete = useCallback(async () => {
    if (!canMutateDocuments) {
      toast.error("Select a vault you administer before deleting documents");
      return;
    }
    setIsBulkDeleting(true);
    try {
      const result = await deleteDocuments(Array.from(selectedIds));
      if (result.deleted_count > 0) {
        toast.success(`Deleted ${result.deleted_count} document${result.deleted_count > 1 ? "s" : ""}`);
        setDocuments((prev) => prev.filter((doc) => !selectedIds.has(doc.id)));
        setStats((prev) =>
          prev
            ? {
                ...prev,
                total_documents: Math.max(0, (prev.total_documents ?? 0) - result.deleted_count),
              }
            : prev
        );
      }
      if (result.failed_ids.length > 0) {
        toast.error(`Failed to delete ${result.failed_ids.length} document${result.failed_ids.length > 1 ? "s" : ""}`);
      }
      clearSelection();
      fetchFolders();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete documents");
    } finally {
      setIsBulkDeleting(false);
    }
  }, [canMutateDocuments, selectedIds, setDocuments, setStats, clearSelection, fetchFolders]);

  const handleBulkDelete = useCallback(() => {
    if (selectedIds.size === 0) return;
    setConfirmDialog({
      open: true,
      title: "Delete Selected Documents",
      description: `Are you sure you want to delete ${selectedIds.size} document${selectedIds.size > 1 ? "s" : ""}? This action cannot be undone.`,
      onConfirm: executeBulkDelete,
      variant: "destructive",
    });
  }, [selectedIds, executeBulkDelete]);

  const executeDeleteAllInVault = useCallback(async () => {
    if (!activeVaultId || !canMutateDocuments) return;
    setIsBulkDeletingAll(true);
    try {
      const result = await deleteAllDocumentsInVault(activeVaultId);
      if (result.deleted_count > 0) {
        toast.success(`Deleted ${result.deleted_count} document${result.deleted_count > 1 ? "s" : ""}`);
        setDocuments([]);
        setStats((prev) => (prev ? { ...prev, total_documents: 0 } : prev));
        fetchFolders();
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete documents");
    } finally {
      setIsBulkDeletingAll(false);
    }
  }, [activeVaultId, canMutateDocuments, setDocuments, setStats, fetchFolders]);

  const handleDeleteAllInVault = useCallback(() => {
    if (!documents || documents.length === 0) return;
    if (!canMutateDocuments) {
      toast.error("Select a vault you administer before deleting documents");
      return;
    }
    setConfirmDialog({
      open: true,
      title: "Delete All Documents in Vault",
      description: "Are you sure you want to delete ALL documents in this vault? This action cannot be undone.",
      onConfirm: executeDeleteAllInVault,
      variant: "destructive",
    });
  }, [documents, canMutateDocuments, executeDeleteAllInVault]);

  const handleFiles = useCallback(
    (acceptedFiles: File[]) => {
      if (!hasSelectedVault || !canWriteActiveVault) {
        toast.error("Select a vault with write access before uploading");
        return;
      }
      const oversizedFiles = acceptedFiles.filter(isUploadTooLarge);
      if (oversizedFiles.length > 0) {
        const rejected = oversizedFiles.map((file) => uploadSizeExceededMessage(file.name));
        setRejectedFiles(rejected);
        rejected.forEach((message) => toast.error(`File rejected: ${message}`));
      } else {
        setRejectedFiles([]);
      }
      const queueableFiles = acceptedFiles.filter((file) => !isUploadTooLarge(file));
      if (queueableFiles.length === 0) return;
      addUploads(queueableFiles, activeVaultId ?? undefined);
      toast.success(`Added ${queueableFiles.length} file(s) to upload queue`);
    },
    [addUploads, activeVaultId, canWriteActiveVault, hasSelectedVault]
  );

  const handleRejected = useCallback((names: string[]) => {
    setRejectedFiles(names);
    names.forEach((name) => toast.error(`File rejected: ${name}`));
  }, []);

  const handleScan = async () => {
    if (!hasSelectedVault || !canWriteActiveVault) {
      toast.error("Select a vault with write access before scanning");
      return;
    }
    setIsScanning(true);
    try {
      const result = await scanDocuments(activeVaultId ?? undefined);
      toast.success(`Scan complete: ${result.added} new document(s) added, ${result.scanned} scanned`);
      await Promise.all([fetchDocuments(), fetchStats()]);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Scan failed");
    } finally {
      setIsScanning(false);
    }
  };

  const handleDeleteDocument = (docId: string) => {
    if (!canMutateDocuments) {
      toast.error("Select a vault you administer before deleting documents");
      return;
    }
    setConfirmDialog({
      open: true,
      title: "Delete Document",
      description: "Are you sure you want to delete this document? This will also remove all associated chunks.",
      onConfirm: () => {
        if (pendingDeleteTimersRef.current.has(docId)) return;

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
    () => documents?.filter((doc) => !optimisticallyDeletedIds.has(doc.id)) ?? [],
    [documents, optimisticallyDeletedIds]
  );
  const hasActiveSearch = searchQuery.trim().length > 0;
  const hasStats = stats !== null;
  const totalVaultDocuments = stats?.total_documents ?? 0;
  const isResolvingSearchEmptyState = hasSelectedVault && hasActiveSearch && !hasStats;
  const emptyState =
    vaults.length === 0
      ? {
          title: "No vaults available",
          description: "Create a vault or ask an admin to grant you access to start uploading documents.",
        }
      : !hasSelectedVault
        ? {
            title: "Select a vault to view documents",
            description: "Documents are scoped to the active vault.",
          }
        : hasActiveSearch && totalVaultDocuments > 0
          ? {
              title: "No documents match your search",
              description: "Search checks filename, type, status, source, sender, subject, and document date.",
            }
          : {
              title: "No documents yet",
              description: canWriteActiveVault
                ? "Upload files to get started."
                : "Documents will appear here when this vault has indexed files.",
            };

  const selectedFileIds = useMemo(
    () => Array.from(selectedIds).map((id) => Number(id)),
    [selectedIds]
  );

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Documents</h1>
          <p className="text-muted-foreground mt-1">Manage your knowledge base documents</p>
        </div>
        <div className="flex items-center gap-2">
          <VaultSelector />
          <Button
            onClick={handleScan}
            disabled={isScanning || !hasSelectedVault || !canWriteActiveVault}
            title={
              !hasSelectedVault
                ? "Select a vault to scan documents"
                : !canWriteActiveVault
                  ? "Write access is required to scan this vault"
                  : "Scan directory"
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
        </div>
      </div>

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
          {selectedIds.size > 0 && canMutateDocuments && (
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
              <Button variant="destructive" size="sm" onClick={handleBulkDelete} disabled={isBulkDeleting}>
                {isBulkDeleting ? (
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                ) : (
                  <Trash2 className="w-4 h-4 mr-2" />
                )}
                Delete Selected
              </Button>
            </>
          )}
          <Badge variant="secondary">{filteredDocuments.length} documents</Badge>
        </div>
      </div>

      <div className="flex gap-6">
        {hasSelectedVault && (
          <FolderTree
            folders={folders}
            selectedFolderId={folderFilterId}
            onSelect={setFolderFilterId}
            canMutate={canMutateDocuments}
            onCreate={handleCreateFolder}
            onRename={handleRenameFolder}
            onDelete={handleDeleteFolder}
            onMove={handleMoveFolderTrigger}
          />
        )}
        <div className="flex-1 min-w-0">
      {loading ? (
        <DocumentsTableSkeleton />
      ) : isResolvingSearchEmptyState ? (
        <div className="space-y-3" role="status" aria-live="polite">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-4 w-96 max-w-full" />
        </div>
      ) : filteredDocuments.length === 0 ? (
        <EmptyState icon={FileText} title={emptyState.title} description={emptyState.description} />
      ) : (
        <>
          <DocumentTable
            documents={filteredDocuments}
            selectedIds={selectedIds}
            canMutateDocuments={canMutateDocuments}
            filenameColWidth={filenameColWidth}
            onResizeMouseDown={handleResizeMouseDown}
            onSelectAll={handleSelectAll}
            onSelectOne={selectOne}
            wikiStatusMap={wikiStatusMap}
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
            canMutateDocuments={canMutateDocuments}
            selectedIds={selectedIds}
            onSelectOne={selectOne}
            onDelete={handleDeleteDocument}
            onDownload={handleDownloadDocument}
          />
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
