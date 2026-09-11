import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useVaultStore } from "@/stores/useVaultStore";
import { VaultSelector } from "@/components/vault/VaultSelector";
import { WikiPageList, PAGE_TYPES } from "./WikiPageList";
import { WikiPageDetail } from "./WikiPageDetail";
import { WikiEditDialog } from "./WikiEditDialog";
import { WikiLintPanel } from "./WikiLintPanel";
import { WikiJobsPanel } from "./WikiJobsPanel";
import { useWikiData } from "@/hooks/useWikiData";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AlertCircle, Layers, Search, Plus, Activity } from "lucide-react";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import { EmptyState } from "@/components/EmptyState";
import { getWikiActivityFeed } from "@/lib/api";
import { useWikiEventStream } from "@/hooks/useWikiEventStream";

/**
 * AC39 (#515): read the router's search params when mounted inside a <Router>,
 * returning null when rendered outside one (bare unit-test mounts). The hook
 * call itself always runs — react-router throws only AFTER its internal
 * useContext, so catching leaves the hook order stable across renders.
 */
function useOptionalSearchParams(): [
  URLSearchParams | null,
  ((next: URLSearchParams, opts?: { replace?: boolean }) => void) | null,
] {
  try {
    const [params, setParams] = useSearchParams();
    return [params, (next, opts) => setParams(next, opts)];
  } catch {
    return [null, null];
  }
}

export default function WikiPage() {
  const { activeVaultId } = useVaultStore();
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editingPage, setEditingPage] = useState<import("@/lib/api").WikiPage | null>(null);
  const [lintPanelOpen, setLintPanelOpen] = useState(false);
  const [jobsPanelOpen, setJobsPanelOpen] = useState(false);
  const [activityPanelOpen, setActivityPanelOpen] = useState(false);
  const [activityEntries, setActivityEntries] = useState<
    Array<{ id: number; action: string; page_title?: string; user?: string; created_at: string }>
  >([]);
  const [activityLoading, setActivityLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [activeType, setActiveType] = useState("");
  const [jobsRefreshSignal, setJobsRefreshSignal] = useState(0);
  const [searchParams, setSearchParams] = useOptionalSearchParams();

  const {
    pages,
    selectedPage,
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
    fetchClaims,
    fetchLintFindings,
    runLint,
  } = useWikiData(activeVaultId);

  useEffect(() => {
    if (activeVaultId) {
      fetchPages();
      fetchLintFindings();
      // AC43 (#515): vault-wide claims surface on the wiki landing view.
      fetchClaims?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeVaultId]);

  useEffect(() => {
    if (activeVaultId) {
      fetchPages({ page_type: activeType || undefined, search: search || undefined });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeType]);

  // AC33 (#515): the terminal-job refetch must keep the user's ACTIVE
  // search/page_type filters. The stream callback is stable, so it reads the
  // current filters through a ref mirror instead of a stale closure.
  const filtersRef = useRef({ search: "", page_type: "" });
  useEffect(() => {
    filtersRef.current = { search, page_type: activeType };
  }, [search, activeType]);

  // AC39 (#515): /wiki?page=<id|slug> deep link — WikiCards navigates here
  // with `page_id` (or slug). Opens the detail without a list click, once per
  // mount; Back clears the param so popstate returns to the list.
  const deepLinkHandledRef = useRef(false);
  // PRR-004 (#531): switching vaults re-arms the one-shot deep link so the
  // still-present ?page= param opens in the NEW vault's context. Declared
  // BEFORE the deep-link effect so the reset lands before it runs; within a
  // single vault the ref still guarantees once-per-mount handling (no
  // re-trigger for the same URL param).
  useEffect(() => {
    deepLinkHandledRef.current = false;
  }, [activeVaultId]);
  useEffect(() => {
    if (!activeVaultId || deepLinkHandledRef.current) return;
    const raw = searchParams?.get("page");
    if (!raw) return;
    const numericId = Number.parseInt(raw, 10);
    if (Number.isInteger(numericId) && numericId > 0) {
      deepLinkHandledRef.current = true;
      openPage(numericId);
      return;
    }
    // Slug form — resolve against the loaded list (best effort).
    const bySlug = pages.find((p) => p.slug === raw);
    if (bySlug) {
      deepLinkHandledRef.current = true;
      openPage(bySlug.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeVaultId, searchParams, pages]);

  const handleBack = useCallback(() => {
    if (searchParams?.has("page") && setSearchParams) {
      const next = new URLSearchParams(searchParams);
      next.delete("page");
      setSearchParams(next, { replace: true });
    }
    closePage();
  }, [searchParams, setSearchParams, closePage]);

  // Subscribe to wiki compile job completion events for the active vault.
  // On any terminal job event, refetch pages, lint findings, and bump the
  // refresh signal so an open WikiJobsPanel reloads too. Uses an authenticated
  // fetch stream (Bearer header) rather than EventSource — see
  // useWikiEventStream for why EventSource always 401s here.
  const handleJobTerminal = useCallback(() => {
    const current = filtersRef.current;
    fetchPages({
      page_type: current.page_type || undefined,
      search: current.search || undefined,
    });
    fetchLintFindings();
    setJobsRefreshSignal((n) => n + 1);
  }, [fetchPages, fetchLintFindings]);
  useWikiEventStream(activeVaultId, handleJobTerminal);

  // Fetch activity feed when panel opens
  useEffect(() => {
    if (!activityPanelOpen || !activeVaultId) return;
    setActivityLoading(true);
    getWikiActivityFeed(activeVaultId, 50)
      .then((data) => setActivityEntries(Array.isArray(data) ? data : data.entries ?? []))
      .catch(() => setActivityEntries([]))
      .finally(() => setActivityLoading(false));
  }, [activityPanelOpen, activeVaultId]);

  function handleCreateClick() {
    setEditingPage(null);
    setEditDialogOpen(true);
  }

  function handleEditClick() {
    if (selectedPage) {
      setEditingPage(selectedPage);
      setEditDialogOpen(true);
    }
  }

  async function handleSave(
    data: Parameters<typeof createPage>[0] | Parameters<typeof editPage>[1],
  ): Promise<void | { conflict: boolean }> {
    if (!activeVaultId) return;
    if (editingPage) {
      // DD-C020 optimistic locking (issue #276 1X-1): send the version we
      // loaded so the backend (wiki.py:246/252) can reject with HTTP 409 if
      // another edit landed first. apiClient does not retry 409.
      try {
        await editPage(editingPage.id, {
          ...(data as Parameters<typeof editPage>[1]),
          expected_version: editingPage.version,
        });
        toast.success("Page updated");
      } catch (err: unknown) {
        const status = (err as { response?: { status?: number } } | undefined)?.response?.status
          ?? (err as { status?: number } | undefined)?.status;
        if (status === 409) {
          toast.error("This page was edited by someone else. Refresh and try again.");
          await fetchPages({ page_type: activeType || undefined, search: search || undefined });
          // AC32 (#515): signal the conflict instead of throwing so the edit
          // dialog keeps the user's draft open (it decides from the signal).
          return { conflict: true };
        }
        throw err;
      }
    } else {
      const createData = data as Parameters<typeof createPage>[0];
      await createPage({ ...createData, vault_id: activeVaultId });
      toast.success("Page created");
    }
    await fetchPages({ page_type: activeType || undefined, search: search || undefined });
  }

  async function handleDelete() {
    if (!selectedPage) return;
    if (!window.confirm(`Delete "${selectedPage.title}"?`)) return;
    await removePage(selectedPage.id);
    toast.success("Page deleted");
  }

  async function handleRunLint() {
    if (!activeVaultId) return;
    const findings = await runLint();
    setLintPanelOpen(true);
    toast.info(`Lint complete: ${findings.length} finding(s)`);
  }

  // AC35 (#515): after resolve/dismiss, refresh the panel from the findings
  // LIST (which reflects suppressions) instead of re-RUNNING lint — a plain
  // re-run must not be able to resurrect a finding the user just dismissed.
  const handleLintRefresh = useCallback(() => {
    if (activeVaultId) fetchLintFindings();
  }, [activeVaultId, fetchLintFindings]);

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between shrink-0">
        <div className="flex items-center gap-3">
          <PageTitleHeader title="Wiki" description="Knowledge base pages" />
        </div>
        <div className="flex items-center gap-2">
          <VaultSelector />
          <Button
            variant="outline"
            onClick={() => { setActivityPanelOpen((v) => !v); setJobsPanelOpen(false); setLintPanelOpen(false); }}
          >
            <Activity className="w-4 h-4 mr-1" />
            Activity
          </Button>
          <Button
            variant="outline"
            onClick={() => { setJobsPanelOpen((v) => !v); setLintPanelOpen(false); setActivityPanelOpen(false); }}
          >
            <Layers className="w-4 h-4 mr-1" />
            Jobs
          </Button>
          <Button
            variant="outline"
            onClick={() => { setLintPanelOpen((v) => !v); setJobsPanelOpen(false); setActivityPanelOpen(false); }}
          >
            <AlertCircle className="w-4 h-4 mr-1" />
            Lint {lintFindings.length > 0 && `(${lintFindings.length})`}
          </Button>
          <Button onClick={handleCreateClick}>
            <Plus className="size-4 mr-1" />
            New Page
          </Button>
        </div>
      </div>

      {/* Toolbar */}
      {activeVaultId && (
        <div className="flex flex-col gap-4">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 w-1/2 relative">
              <Search
                className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />
              <Input
                placeholder="Search wiki..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && fetchPages({ page_type: activeType || undefined, search: search || undefined })}
                className="w-full pl-10"
              />
              <Button
                variant="outline"
                size="icon"
                onClick={() => fetchPages({ page_type: activeType || undefined, search: search || undefined })}
                aria-label="Search"
              >
                <Search className="size-4" />
              </Button>
            </div>
          </div>
          <Tabs value={activeType} onValueChange={setActiveType}>
            <TabsList className="flex-wrap h-auto gap-1">
              {PAGE_TYPES.map((t) => (
                <TabsTrigger key={t.value} value={t.value} className="text-xs">
                  {t.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>
      )}

      {/* Main content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left panel: list */}
        <div
          className={`flex flex-col overflow-hidden ${
            selectedPage ? "border-r border-border hidden md:flex md:w-80 lg:w-96 shrink-0 pr-4" : "flex-1"
          }`}
        >
          {!activeVaultId ? (
            <EmptyState
              title="Select a vault"
              description="Choose a vault to view its wiki pages."
            />
          ) : (
            <WikiPageList
              pages={pages}
              loading={loading}
              onSelect={openPage}
              vaultId={activeVaultId}
              onRefresh={() => fetchPages({ page_type: activeType || undefined, search: search || undefined })}
              hasMore={pages.length < total}
              loadingMore={loadingMore}
              onLoadMore={loadMore ? () => loadMore({ page_type: activeType || undefined, search: search || undefined }) : undefined}
            />
          )}
          {error && (
            <p className="text-sm text-destructive mt-2">{error}</p>
          )}
        </div>

        {/* Right panel: detail */}
        {selectedPage && (
          <div className="flex-1 px-4 overflow-hidden">
            <WikiPageDetail
              page={selectedPage}
              onBack={handleBack}
              onEdit={handleEditClick}
              onDelete={handleDelete}
            />
          </div>
        )}

        {/* Lint panel: right side overlay */}
        {lintPanelOpen && (
          <div className="w-80 border-l border-border p-4 overflow-y-auto shrink-0">
            <WikiLintPanel
              findings={lintFindings}
              loading={loading}
              onRunLint={handleRunLint}
              onRefresh={handleLintRefresh}
              vaultId={activeVaultId}
            />
          </div>
        )}

        {/* Jobs panel: right side overlay */}
        {jobsPanelOpen && activeVaultId && (
          <div className="w-80 border-l border-border p-4 overflow-y-auto shrink-0">
            <WikiJobsPanel vaultId={activeVaultId} refreshSignal={jobsRefreshSignal} />
          </div>
        )}

        {/* Activity panel: right side overlay */}
        {activityPanelOpen && activeVaultId && (
          <div className="w-80 border-l border-border p-4 overflow-y-auto shrink-0">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold">Activity Feed</h3>
            </div>
            {activityLoading && <p className="text-xs text-muted-foreground">Loading...</p>}
            {!activityLoading && activityEntries.length === 0 && (
              <p className="text-xs text-muted-foreground">No recent activity.</p>
            )}
            {!activityLoading && activityEntries.length > 0 && (
              <div className="flex flex-col gap-2">
                {activityEntries.map((entry) => (
                  <div key={entry.id} className="rounded-md border border-border px-3 py-2 text-xs">
                    <div className="font-medium capitalize">{entry.action.replace(/_/g, " ")}</div>
                    {entry.page_title && (
                      <div className="text-muted-foreground truncate">{entry.page_title}</div>
                    )}
                    <div className="flex items-center gap-2 mt-1 text-muted-foreground">
                      {entry.user && <span>{entry.user}</span>}
                      <span>{new Date(entry.created_at).toLocaleString()}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Vault-wide claims (AC43, issue #515): every claim in the vault with
          its lifecycle status. Empty state is explicit so a claimless vault is
          clearly distinguishable from a loading one. */}
      <Card>
        <CardHeader className="pb-2 pt-3 px-4">
          <CardTitle className="text-sm">Claims</CardTitle>
        </CardHeader>
        <CardContent className="px-4 pb-3">
          {claims && claims.length > 0 ? (
            <div className="flex flex-col gap-2">
              {claims.map((claim) => (
                <div
                  key={claim.id}
                  className="flex items-start justify-between gap-2 border-b border-border pb-2 last:border-0 last:pb-0"
                >
                  <p className="text-sm min-w-0">{claim.claim_text}</p>
                  <Badge variant="outline" className="text-[10px] uppercase shrink-0">
                    {claim.status.replace(/_/g, " ")}
                  </Badge>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">No claims yet.</p>
          )}
        </CardContent>
      </Card>

      {/* Lifecycle help (AC44, issue #515): how the knowledge surfaces relate
          and where promoted knowledge ends up. */}
      <Card>
        <CardHeader className="pb-2 pt-3 px-4">
          <CardTitle className="text-sm">How knowledge works</CardTitle>
        </CardHeader>
        <CardContent className="px-4 pb-3 text-xs text-muted-foreground space-y-1">
          <p>
            <strong className="text-foreground">Documents</strong> are ingested files —
            the raw source of truth for everything extracted downstream.
          </p>
          <p>
            <strong className="text-foreground">Memories</strong> capture durable facts from
            chats and notes; interesting ones can be promoted into wiki pages.
          </p>
          <p>
            <strong className="text-foreground">Wiki pages</strong> compile claims and
            entities extracted from documents and memories into readable knowledge.
          </p>
          <p>
            <strong className="text-foreground">KMS entries</strong> are curated how-to
            knowledge maintained for reuse across the vault.
          </p>
          <p>
            Promotion outcomes: extracted claims land with a lifecycle status
            (active, awaiting review, superseded, …); documents with no
            extractable knowledge are marked skipped; stale claims are flagged
            by lint for review.
          </p>
        </CardContent>
      </Card>

      {/* Edit / Create dialog */}
      {activeVaultId && (
        <WikiEditDialog
          open={editDialogOpen}
          page={editingPage}
          vaultId={activeVaultId}
          onClose={() => setEditDialogOpen(false)}
          onSave={handleSave}
        />
      )}
    </div>
  );
}
