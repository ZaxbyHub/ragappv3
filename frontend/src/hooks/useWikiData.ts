import { useState, useCallback, useRef } from "react";
import {
  listWikiPages,
  getWikiPage,
  createWikiPage,
  updateWikiPage,
  deleteWikiPage,
  listWikiEntities,
  listWikiClaims,
  listWikiLintFindings,
  runWikiLint,
  searchWiki,
  type WikiPage,
  type WikiEntity,
  type WikiClaim,
  type WikiLintFinding,
} from "@/lib/api";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockWikiPages, mockWikiLintFindings } from "@/fixtures/wiki";

/** AC34 (#515): page size used for Load-more requests (matches the backend default). */
const LIST_PER_PAGE = 50;

export function useWikiData(vaultId: number | null) {
  const testMode = useTestMode();
  const [pages, setPages] = useState<WikiPage[]>(testMode ? mockWikiPages : []);
  const [selectedPage, setSelectedPage] = useState<WikiPage | null>(null);
  const [entities, setEntities] = useState<WikiEntity[]>([]);
  const [claims, setClaims] = useState<WikiClaim[]>([]);
  const [lintFindings, setLintFindings] = useState<WikiLintFinding[]>(testMode ? mockWikiLintFindings : []);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // AC34 (#515): total rows matching the current filters (backend list route).
  const [total, setTotal] = useState<number>(0);

  // AC31 (#515) request-identity guards. Each list/detail request captures the
  // current generation; a response may only commit state while it is still the
  // NEWEST request of its kind. A slow earlier response resolving last can
  // therefore never clobber a newer one (list half and detail half).
  const listGenRef = useRef(0);
  const detailGenRef = useRef(0);
  // Current list page for Load-more (AC34) — a ref so loadMore reads the
  // latest committed page without re-creating the callback.
  const listPageRef = useRef(1);

  const fetchPages = useCallback(
    async (params?: { page_type?: string; status?: string; search?: string }) => {
      if (!vaultId) return;
      if (testMode) {
        let filtered = mockWikiPages;
        if (params?.page_type) {
          filtered = filtered.filter((p) => p.page_type === params.page_type);
        }
        if (params?.status) {
          filtered = filtered.filter((p) => p.status === params.status);
        }
        if (params?.search) {
          const q = params.search.toLowerCase();
          filtered = filtered.filter((p) => p.title.toLowerCase().includes(q) || p.summary?.toLowerCase().includes(q));
        }
        setPages(filtered);
        listPageRef.current = 1;
        return;
      }
      const gen = ++listGenRef.current;
      setLoading(true);
      setError(null);
      try {
        const res = await listWikiPages({ vault_id: vaultId, ...params });
        if (listGenRef.current !== gen) return;
        setPages(res.pages);
        listPageRef.current = res.page ?? 1;
        setTotal(typeof res.total === "number" ? res.total : res.pages.length);
      } catch (e) {
        if (listGenRef.current !== gen) return;
        setError(e instanceof Error ? e.message : "Failed to load pages");
      } finally {
        if (listGenRef.current === gen) setLoading(false);
      }
    },
    [vaultId, testMode]
  );

  // AC34 (#515): fetch the NEXT page with the same filters and append. Shares
  // the list generation guard so a Load-more response never clobbers a newer
  // full refetch (and vice versa).
  const loadMore = useCallback(
    async (params?: { page_type?: string; status?: string; search?: string }) => {
      if (!vaultId || testMode) return;
      const nextPage = listPageRef.current + 1;
      const gen = ++listGenRef.current;
      setLoadingMore(true);
      try {
        const res = await listWikiPages({
          vault_id: vaultId,
          ...params,
          page: nextPage,
          per_page: LIST_PER_PAGE,
        });
        if (listGenRef.current !== gen) return;
        setPages((prev) => [...prev, ...res.pages]);
        listPageRef.current = nextPage;
        setTotal((prevTotal) => (typeof res.total === "number" ? res.total : prevTotal));
      } catch (e) {
        if (listGenRef.current !== gen) return;
        setError(e instanceof Error ? e.message : "Failed to load more pages");
      } finally {
        if (listGenRef.current === gen) setLoadingMore(false);
      }
    },
    [vaultId, testMode]
  );

  const openPage = useCallback(async (pageId: number) => {
    if (testMode) {
      const page = mockWikiPages.find((p) => p.id === pageId) ?? null;
      setSelectedPage(page);
      return;
    }
    const gen = ++detailGenRef.current;
    setLoading(true);
    setError(null);
    try {
      const page = await getWikiPage(pageId);
      if (detailGenRef.current !== gen) return;
      setSelectedPage(page);
    } catch (e) {
      if (detailGenRef.current !== gen) return;
      setError(e instanceof Error ? e.message : "Failed to load page");
    } finally {
      if (detailGenRef.current === gen) setLoading(false);
    }
  }, [testMode]);

  // Back invalidates any in-flight detail request so a late response cannot
  // reopen the page the user just left (AC31 detail half).
  const closePage = useCallback(() => {
    detailGenRef.current += 1;
    setSelectedPage(null);
  }, []);

  const createPage = useCallback(
    async (data: Parameters<typeof createWikiPage>[0]) => {
      const page = await createWikiPage(data);
      setPages((prev) => [page, ...prev]);
      return page;
    },
    []
  );

  const editPage = useCallback(
    async (pageId: number, data: Parameters<typeof updateWikiPage>[1]) => {
      const updated = await updateWikiPage(pageId, data);
      setPages((prev) => prev.map((p) => (p.id === pageId ? updated : p)));
      if (selectedPage?.id === pageId) setSelectedPage(updated);
      return updated;
    },
    [selectedPage]
  );

  const removePage = useCallback(
    async (pageId: number) => {
      await deleteWikiPage(pageId);
      setPages((prev) => prev.filter((p) => p.id !== pageId));
      if (selectedPage?.id === pageId) setSelectedPage(null);
    },
    [selectedPage]
  );

  const fetchEntities = useCallback(
    async (search?: string) => {
      if (!vaultId) return;
      const res = await listWikiEntities({ vault_id: vaultId, search });
      setEntities(res.entities);
    },
    [vaultId]
  );

  const fetchClaims = useCallback(
    async (params?: { page_id?: number; search?: string; status?: string }) => {
      if (!vaultId) return;
      const res = await listWikiClaims({ vault_id: vaultId, ...params });
      setClaims(res.claims);
    },
    [vaultId]
  );

  const fetchLintFindings = useCallback(async () => {
    if (!vaultId) return;
    if (testMode) {
      setLintFindings(mockWikiLintFindings);
      return;
    }
    const res = await listWikiLintFindings({ vault_id: vaultId });
    setLintFindings(res.findings);
  }, [vaultId, testMode]);

  const runLint = useCallback(async () => {
    if (!vaultId) return [];
    setLoading(true);
    try {
      const res = await runWikiLint(vaultId);
      setLintFindings(res.findings);
      return res.findings;
    } finally {
      setLoading(false);
    }
  }, [vaultId]);

  const search = useCallback(
    async (q: string) => {
      if (!vaultId || !q.trim()) return null;
      return searchWiki({ vault_id: vaultId, q });
    },
    [vaultId]
  );

  return {
    pages,
    selectedPage,
    entities,
    claims,
    lintFindings,
    loading,
    loadingMore,
    error,
    total,
    fetchPages,
    loadMore,
    openPage,
    closePage,
    createPage,
    editPage,
    removePage,
    fetchEntities,
    fetchClaims,
    fetchLintFindings,
    runLint,
    search,
  };
}
