import { useState, useCallback } from "react";
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

export function useWikiData(vaultId: number | null) {
  const testMode = useTestMode();
  const [pages, setPages] = useState<WikiPage[]>(testMode ? mockWikiPages : []);
  const [selectedPage, setSelectedPage] = useState<WikiPage | null>(null);
  const [entities, setEntities] = useState<WikiEntity[]>([]);
  const [claims, setClaims] = useState<WikiClaim[]>([]);
  const [lintFindings, setLintFindings] = useState<WikiLintFinding[]>(testMode ? mockWikiLintFindings : []);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
        return;
      }
      setLoading(true);
      setError(null);
      try {
        const res = await listWikiPages({ vault_id: vaultId, ...params });
        setPages(res.pages);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load pages");
      } finally {
        setLoading(false);
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
    setLoading(true);
    setError(null);
    try {
      const page = await getWikiPage(pageId);
      setSelectedPage(page);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load page");
    } finally {
      setLoading(false);
    }
  }, [testMode]);

  const closePage = useCallback(() => setSelectedPage(null), []);

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
    error,
    fetchPages,
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
