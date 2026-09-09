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
  // Monotonic generation for list fetches: only the most recently issued
  // fetchDocuments may commit. A late response (success or rejection) for an
  // abandoned query must never overwrite or clear a newer query's results.
  const fetchGenerationRef = useRef(0);
  // Same ordering guarantee for wiki-status fetches (hydration effect vs the
  // poll-to-terminal interval; post-unmount commits are dropped).
  const wikiFetchAttemptRef = useRef(0);

  const fetchDocuments = useCallback(async () => {
    const generation = ++fetchGenerationRef.current;
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
      if (fetchGenerationRef.current !== generation) return;
      setDocuments(response?.documents || []);
      setTotal(response?.total ?? 0);
    } catch (err) {
      if (fetchGenerationRef.current !== generation) return;
      console.error("Failed to fetch documents:", err);
      toast.error(err instanceof Error ? err.message : "Failed to load documents");
      setDocuments([]);
      setTotal(0);
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
      // Monotonic attempt token (mirrors fetchDocuments' generation guard):
      // only the most recently issued fetchWikiStatuses may commit, so a
      // hydration effect and the poll-to-terminal interval can interleave
      // without a stale response regressing a newer wiki status, and an
      // in-flight fetch that resolves after unmount commits nothing.
      const attempt = ++wikiFetchAttemptRef.current;
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
      if (wikiFetchAttemptRef.current !== attempt) return;
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

  // Hydrate wiki statuses when the doc-ID set changes (best-effort). Keyed on
  // the ID join — NOT the array identity — so the adaptive status poll's
  // fetchDocuments() refresh (new array, same IDs) doesn't refire a wiki GET
  // per document on every cycle. Only docs with no cached status or a
  // transient status (compiling) are re-polled; terminal wiki statuses don't
  // change without user action.
  const wikiDocKey = documents.map((d) => String(d.id)).join(",");
  useEffect(() => {
    if (documents.length === 0) return;
    const need = documents.filter((d) => {
      if (d.metadata?.status !== "indexed") return false;
      const w = wikiStatusMap[String(d.id)];
      return !w || isTransientWikiStatus(w.wiki_status);
    });
    if (need.length > 0) fetchWikiStatuses(need);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wikiDocKey, fetchWikiStatuses]);

  // Independent poll-to-terminal for transient wiki statuses (e.g. compiling):
  // while any listed doc's CACHED wiki status is transient, re-run the batched
  // fetchWikiStatuses on a fixed ~5s cadence until every transient status
  // reaches a terminal state. This is decoupled from document-list changes and
  // from the indexing poller above — a compiling wiki can finish long after
  // the document itself is indexed, and the list must not be refetched for the
  // wiki cell to advance. One fetch per tick, skipped while a fetch is already
  // in flight, and the interval is cancelled on unmount or when nothing is
  // transient anymore.
  const WIKI_POLL_INTERVAL_MS = 5_000;
  const transientWikiKey = documents
    .filter(
      (d) =>
        d.metadata?.status === "indexed" &&
        isTransientWikiStatus(wikiStatusMap[String(d.id)]?.wiki_status)
    )
    .map((d) => String(d.id))
    .join(",");
  const wikiFetchInFlightRef = useRef(false);
  useEffect(() => {
    if (!transientWikiKey) return;
    const transientDocs = documents.filter((d) =>
      transientWikiKey.split(",").includes(String(d.id))
    );
    const timer = setInterval(() => {
      if (wikiFetchInFlightRef.current) return;
      wikiFetchInFlightRef.current = true;
      void fetchWikiStatuses(transientDocs).finally(() => {
        wikiFetchInFlightRef.current = false;
      });
    }, WIKI_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transientWikiKey, fetchWikiStatuses]);

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

/**
 * Transient wiki statuses are still moving toward a terminal state and must be
 * re-polled; terminal statuses (compiled/failed/not_compiled/skipped) only
 * change through user action.
 */
function isTransientWikiStatus(status: string | null | undefined): boolean {
  return status === "compiling" || status === "running";
}
