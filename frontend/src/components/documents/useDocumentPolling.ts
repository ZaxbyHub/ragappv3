import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  listDocuments,
  getDocumentStats,
  getDocumentWikiStatus,
  compileDocumentWiki,
  type Document,
  type DocumentStatsResponse,
  type DocumentWikiStatus,
  type DocumentSortBy,
  type SortOrder,
} from "@/lib/api";
import type { UploadFile } from "@/stores/useUploadStore";

interface UseDocumentPollingArgs {
  activeVaultId: number | null;
  search: string;
  sortBy: DocumentSortBy;
  sortOrder: SortOrder;
  tagFilterId: number | null;
  folderFilterId: number | null;
  uploads: UploadFile[];
}

/**
 * Owns the document list lifecycle: initial load, query-driven refetch (search,
 * sort, tag filter), adaptive status polling for in-flight documents, wiki
 * status hydration, and refresh-on-upload-complete.
 *
 * The skeleton (`loading`) is only shown on the first load per vault — search,
 * sort, and tag changes refresh in place (the search box surfaces its own
 * pending indicator), matching the pre-refactor behavior.
 */
export function useDocumentPolling({
  activeVaultId,
  search,
  sortBy,
  sortOrder,
  tagFilterId,
  folderFilterId,
  uploads,
}: UseDocumentPollingArgs) {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [stats, setStats] = useState<DocumentStatsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [wikiStatusMap, setWikiStatusMap] = useState<Record<string, DocumentWikiStatus>>({});
  const [compilingDocIds, setCompilingDocIds] = useState<Set<string>>(new Set());
  // Total documents matching the current query (drives the "load more" control),
  // and the size of the fetched window. Rather than accumulate discrete pages
  // (which would fight the status poller that always refetches the window), we
  // fetch a single window of `pageSize` rows and grow it on demand. The poller
  // and search/sort refetch the same window, so the list stays consistent.
  const PAGE_SIZE = 50;
  const [total, setTotal] = useState(0);
  const [pageSize, setPageSize] = useState(PAGE_SIZE);
  const pollIntervalMsRef = useRef(2_000);
  const initialLoadDone = useRef(false);

  const fetchDocuments = useCallback(async () => {
    try {
      const response = await listDocuments({
        vaultId: activeVaultId ?? undefined,
        search: search || undefined,
        sortBy,
        sortOrder,
        tagId: tagFilterId ?? undefined,
        folderId: folderFilterId ?? undefined,
        perPage: pageSize,
      });
      setDocuments(response?.documents || []);
      setTotal(response?.total ?? 0);
    } catch (err) {
      console.error("Failed to fetch documents:", err);
      toast.error(err instanceof Error ? err.message : "Failed to load documents");
      setDocuments([]);
    }
  }, [activeVaultId, search, sortBy, sortOrder, tagFilterId, folderFilterId, pageSize]);

  // Reset the fetch window to the first page whenever the query changes, so a
  // new vault/search/sort/filter starts at 50 rows rather than carrying a large
  // window over. (Capped at the backend per_page max of 1000.)
  useEffect(() => {
    setPageSize(PAGE_SIZE);
  }, [activeVaultId, search, sortBy, sortOrder, tagFilterId, folderFilterId]);

  const loadMore = useCallback(() => {
    setPageSize((prev) => Math.min(prev + PAGE_SIZE, 1000));
  }, []);

  const hasMore = documents.length < total;

  const fetchStats = useCallback(async () => {
    try {
      const response = await getDocumentStats(activeVaultId ?? undefined);
      setStats(response);
    } catch (err) {
      console.error("Failed to fetch stats:", err);
      toast.error(err instanceof Error ? err.message : "Failed to load document stats");
    }
  }, [activeVaultId]);

  const fetchWikiStatuses = useCallback(
    async (docs: Document[]) => {
      if (!activeVaultId) return;
      const indexed = docs.filter((d) => d.metadata?.status === "indexed");
      // Bound concurrency so a large vault (hundreds of indexed docs) cannot
      // fire one request per document simultaneously on every refresh (F-003).
      const CONCURRENCY = 6;
      const results: (DocumentWikiStatus | undefined)[] = new Array(indexed.length);
      let cursor = 0;
      const worker = async () => {
        while (cursor < indexed.length) {
          const i = cursor++;
          try {
            results[i] = await getDocumentWikiStatus(Number(indexed[i].id), activeVaultId);
          } catch {
            results[i] = undefined;
          }
        }
      };
      await Promise.all(
        Array.from({ length: Math.min(CONCURRENCY, indexed.length) }, worker)
      );
      setWikiStatusMap((prev) => {
        const next = { ...prev };
        indexed.forEach((d, i) => {
          const r = results[i];
          if (r !== undefined) next[String(d.id)] = r;
        });
        return next;
      });
    },
    [activeVaultId]
  );

  const handleCompileDocument = useCallback(
    async (docId: string) => {
      if (!activeVaultId) return;
      setCompilingDocIds((prev) => new Set(prev).add(docId));
      try {
        await compileDocumentWiki(Number(docId), activeVaultId);
        toast.success("Wiki compile job queued");
        setTimeout(() => fetchWikiStatuses(documents), 2000);
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Failed to queue wiki compile");
      } finally {
        setCompilingDocIds((prev) => {
          const s = new Set(prev);
          s.delete(docId);
          return s;
        });
      }
    },
    [activeVaultId, documents, fetchWikiStatuses]
  );

  // Reset the skeleton gate on vault switch so the next load shows it, but
  // search/sort/tag changes refresh in place. Declared before the load effect
  // so the ref is reset before the load effect reads it.
  useEffect(() => {
    initialLoadDone.current = false;
  }, [activeVaultId]);

  // Initial + query-driven load. fetchDocuments/fetchStats change together on a
  // vault switch, so this fires once per change (no double-fetch).
  useEffect(() => {
    let cancelled = false;
    const showSkeleton = !initialLoadDone.current;
    (async () => {
      if (showSkeleton) setLoading(true);
      await Promise.all([fetchDocuments(), fetchStats()]);
      if (!cancelled) {
        initialLoadDone.current = true;
        if (showSkeleton) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchDocuments, fetchStats]);

  // Adaptive status polling for in-flight documents. Starts at 2 s and backs
  // off up to 30 s while no change is detected, resetting when idle.
  useEffect(() => {
    const hasProcessingDocs = documents?.some(
      (doc) => doc.metadata?.status === "processing" || doc.metadata?.status === "pending"
    );

    if (!hasProcessingDocs) {
      pollIntervalMsRef.current = 2_000;
      return;
    }

    const delay = pollIntervalMsRef.current;
    const timer = setTimeout(() => {
      pollIntervalMsRef.current = Math.min(pollIntervalMsRef.current * 1.5, 30_000);
      fetchDocuments();
      fetchStats();
    }, delay);

    return () => clearTimeout(timer);
  }, [documents, fetchDocuments, fetchStats]);

  // Hydrate wiki statuses whenever the document list changes (best-effort).
  useEffect(() => {
    if (documents.length > 0) fetchWikiStatuses(documents);
  }, [documents, fetchWikiStatuses]);

  // Refresh documents shortly after uploads finish indexing.
  useEffect(() => {
    const completedCount = uploads.filter((u) => u.status === "indexed").length;
    if (completedCount > 0) {
      const timeout = setTimeout(() => {
        fetchDocuments();
        fetchStats();
      }, 1000);
      return () => clearTimeout(timeout);
    }
  }, [uploads, fetchDocuments, fetchStats]);

  return {
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
    total,
    hasMore,
    loadMore,
  };
}
