import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  FileText,
  Library,
  Loader2,
  Plus,
  RefreshCw,
  Search,
} from "lucide-react";

import { useVaultStore } from "@/stores/useVaultStore";
import { VaultSelector } from "@/components/vault/VaultSelector";
import { VaultGate } from "@/components/vault/VaultGate";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import { EmptyState } from "@/components/EmptyState";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  createKMSEntry,
  listKMSEntries,
  listKMSJobs,
  recompileVaultKMS,
  type KMSEntry,
} from "@/lib/api";
import { isTerminalJobStatus, useJobStatus } from "@/hooks/useJobStatus";

const STATUS_OPTIONS = ["all", "draft", "published", "archived"] as const;

function statusVariant(status: string): "default" | "secondary" | "outline" {
  if (status === "published") return "default";
  if (status === "archived") return "outline";
  return "secondary";
}

export default function KMSPage() {
  const { activeVaultId } = useVaultStore();
  const navigate = useNavigate();

  const [entries, setEntries] = useState<KMSEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");

  const [createOpen, setCreateOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newBody, setNewBody] = useState("");
  const [newTags, setNewTags] = useState("");
  const [saving, setSaving] = useState(false);

  // Generation token (issue #515 / C36): a slow earlier list/search response
  // resolving after a newer request must never commit stale entries or total.
  // Mirrors KMSDetailPage's loadGenRef pattern — checked after the await and
  // on the error/finally paths so late responses are dropped entirely.
  const fetchGenRef = useRef(0);

  const fetchEntries = useCallback(
    async (pageToLoad: number, append: boolean) => {
      if (!activeVaultId) return;
      const gen = ++fetchGenRef.current;
      setLoading(true);
      setError(null);
      try {
        const res = await listKMSEntries({
          vault_id: activeVaultId,
          search: search.trim() || undefined,
          status: statusFilter === "all" ? undefined : statusFilter,
          page: pageToLoad,
          per_page: 200,
        });
        if (fetchGenRef.current !== gen) return;
        setEntries((prev) => (append ? [...prev, ...res.entries] : res.entries));
        setTotal(res.total);
        setPage(res.page ?? pageToLoad);
      } catch (e) {
        if (fetchGenRef.current !== gen) return;
        setError(e instanceof Error ? e.message : "Failed to load KMS entries");
        // PRR-002 (#531): re-sync page state on error too — without this a
        // failed load-more left `page` pointing at the page that never loaded.
        setPage(pageToLoad);
      } finally {
        if (fetchGenRef.current === gen) {
          setLoading(false);
        }
      }
    },
    [activeVaultId, search, statusFilter]
  );

  useEffect(() => {
    if (!activeVaultId) return;
    const t = setTimeout(() => fetchEntries(1, false), search ? 300 : 0);
    return () => clearTimeout(t);
  }, [activeVaultId, search, statusFilter, fetchEntries]);

  // Recompile job polling (issue #783): the POST /kms/recompile handle is
  // kept and polled through listKMSJobs until a terminal status, then the
  // entries refetch and the toast reports the real outcome. The listKMSJobs
  // call lives ONLY inside the poll-time fetcher (lazy-closure constraint)
  // so suites mocking @/lib/api with partial factories never dereference it
  // outside a poll.
  const recompileJobIdRef = useRef<number | null>(null);
  const recompilePoll = useJobStatus("kms", {
    fetchJob: async () => {
      const vaultId = activeVaultId;
      const jobId = recompileJobIdRef.current;
      if (vaultId == null || jobId == null) return null;
      const { jobs } = await listKMSJobs(vaultId);
      return jobs.find((j) => j.id === jobId) ?? null;
    },
    onTerminal: (job) => {
      if (job.status === "failed") {
        toast.error(
          job.error ? `Recompile failed: ${job.error}` : "Recompile failed"
        );
      } else if (job.status === "cancelled") {
        toast.info("Recompile cancelled");
      } else {
        toast.success("Recompile complete — entries refreshed");
      }
      void fetchEntries(1, false);
    },
    onTimeout: () => {
      toast.error("Recompile did not finish in time");
    },
  });

  // C37: past the fixed per_page=200 window the remaining entries are fetched
  // on demand; the page-2 request retains the current search/vault filters.
  // PRR-007 (#531): no-op while a fetch is already in flight so a load-more
  // click can't race (and interleave with) a just-started filter change.
  function handleLoadMore() {
    if (!activeVaultId || loading) return;
    fetchEntries(page + 1, true);
  }

  async function handleCreate() {
    if (!activeVaultId || !newTitle.trim()) return;
    setSaving(true);
    try {
      const tags = newTags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);
      const entry = await createKMSEntry({
        vault_id: activeVaultId,
        title: newTitle.trim(),
        body: newBody,
        tags,
      });
      toast.success("Entry created");
      setCreateOpen(false);
      setNewTitle("");
      setNewBody("");
      setNewTags("");
      navigate(`/kms/${entry.id}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to create entry");
    } finally {
      setSaving(false);
    }
  }

  const recompilePostInFlightRef = useRef(false);
  async function handleRecompile() {
    if (!activeVaultId || recompilePoll.active) return;
    if (recompilePostInFlightRef.current) return; // double-click guard (PRR-020)
    recompilePostInFlightRef.current = true;
    try {
      const handle = await recompileVaultKMS(activeVaultId);
      if (isTerminalJobStatus(handle.status)) {
        // Already terminal (e.g. a synchronous compile result): no polling.
        if (handle.status === "failed") {
          toast.error("Recompile failed");
        } else if (handle.status === "cancelled") {
          toast.info("Recompile cancelled");
        } else {
          toast.success("Recompile complete — entries refreshed");
        }
        void fetchEntries(1, false);
        return;
      }
      recompileJobIdRef.current = handle.job_id;
      recompilePoll.start();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to queue recompile");
    } finally {
      recompilePostInFlightRef.current = false;
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-6 py-4 border-b border-border shrink-0">
        <PageTitleHeader
          before={<Library className="w-5 h-5 text-muted-foreground" />}
          title="Knowledge Management"
          actions={
            <>
              <VaultSelector />
              <Button
                variant="outline"
                size="sm"
                onClick={handleRecompile}
                disabled={!activeVaultId || recompilePoll.active}
                aria-busy={recompilePoll.active}
                title={
                  recompilePoll.active
                    ? "Recompile in progress…"
                    : "Recompile document entries for this vault"
                }
              >
                {recompilePoll.active ? (
                  <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                ) : (
                  <RefreshCw className="w-4 h-4 mr-1" />
                )}
                Recompile
              </Button>
              <Button
                size="sm"
                onClick={() => setCreateOpen(true)}
                disabled={!activeVaultId}
              >
                <Plus className="w-4 h-4 mr-1" />
                New entry
              </Button>
            </>
          }
        />
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-2 px-6 py-3 border-b border-border shrink-0">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-2 top-2.5 w-4 h-4 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search title and content…"
            className="pl-8"
            disabled={!activeVaultId}
          />
        </div>
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          {/* Issue #778: the trigger needs an explicit accessible name —
              placeholder-only SelectValue leaves the combobox unnamed
              (axe button-name, critical). */}
          <SelectTrigger className="w-40" aria-label="Status filter">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            {STATUS_OPTIONS.map((s) => (
              <SelectItem key={s} value={s}>
                {s === "all" ? "All statuses" : s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Body */}
      <ScrollArea className="flex-1">
        <div className="p-6">
          {!activeVaultId ? (
            <EmptyState
              title="Select a vault"
              description="Select a vault to view its knowledge entries."
              action={<VaultGate />}
            />
          ) : error ? (
            <p className="text-sm text-destructive py-8">{error}</p>
          ) : loading && entries.length === 0 ? (
            <p className="text-sm text-muted-foreground py-8">Loading…</p>
          ) : entries.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-12">
              No entries yet. Create one, or upload documents to auto-generate
              entries.
            </p>
          ) : (
            <>
              <p className="text-xs text-muted-foreground mb-3">
                {total} {total === 1 ? "entry" : "entries"}
              </p>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {entries.map((entry) => (
                  <button
                    key={entry.id}
                    onClick={() => navigate(`/kms/${entry.id}`)}
                    className="text-left rounded-lg border border-border p-4 hover:border-primary hover:bg-accent/40 transition-colors"
                  >
                    <div className="flex items-start justify-between gap-2 mb-1">
                      <h3 className="font-medium text-sm line-clamp-2">
                        {entry.title}
                      </h3>
                      {entry.source_type === "document" && (
                        <FileText
                          className="w-4 h-4 text-muted-foreground shrink-0"
                          aria-label="Document-sourced entry"
                        />
                      )}
                    </div>
                    {entry.summary && (
                      <p className="text-xs text-muted-foreground line-clamp-3 mb-2">
                        {entry.summary}
                      </p>
                    )}
                    <div className="flex flex-wrap gap-1 items-center">
                      <Badge variant={statusVariant(entry.status)} className="text-[10px]">
                        {entry.status}
                      </Badge>
                      {entry.tags.slice(0, 3).map((tag) => (
                        <Badge key={tag} variant="outline" className="text-[10px]">
                          {tag}
                        </Badge>
                      ))}
                    </div>
                  </button>
                ))}
              </div>
              {entries.length < total && (
                <div className="flex justify-center mt-4">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleLoadMore}
                    disabled={loading || !activeVaultId}
                  >
                    Load more
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      </ScrollArea>

      {/* Create dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New knowledge entry</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-3">
            <div>
              <Label htmlFor="kms-title">Title</Label>
              <Input
                id="kms-title"
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                placeholder="Entry title"
                // eslint-disable-next-line jsx-a11y-x/no-autofocus -- Intentional first-field focus when the create-entry dialog opens.
                autoFocus
              />
            </div>
            <div>
              <Label htmlFor="kms-body">Body (markdown)</Label>
              <Textarea
                id="kms-body"
                value={newBody}
                onChange={(e) => setNewBody(e.target.value)}
                placeholder="Write the entry content…"
                rows={8}
              />
            </div>
            <div>
              <Label htmlFor="kms-tags">Tags (comma-separated)</Label>
              <Input
                id="kms-tags"
                value={newTags}
                onChange={(e) => setNewTags(e.target.value)}
                placeholder="onboarding, policy"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleCreate} disabled={saving || !newTitle.trim()}>
              {saving ? "Creating…" : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
