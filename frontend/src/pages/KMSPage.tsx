import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { FileText, Library, Plus, RefreshCw, Search } from "lucide-react";

import { useVaultStore } from "@/stores/useVaultStore";
import { VaultSelector } from "@/components/vault/VaultSelector";
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
  recompileVaultKMS,
  type KMSEntry,
} from "@/lib/api";

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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");

  const [createOpen, setCreateOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newBody, setNewBody] = useState("");
  const [newTags, setNewTags] = useState("");
  const [saving, setSaving] = useState(false);

  const fetchEntries = useCallback(async () => {
    if (!activeVaultId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await listKMSEntries({
        vault_id: activeVaultId,
        search: search.trim() || undefined,
        status: statusFilter === "all" ? undefined : statusFilter,
        per_page: 200,
      });
      setEntries(res.entries);
      setTotal(res.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load KMS entries");
    } finally {
      setLoading(false);
    }
  }, [activeVaultId, search, statusFilter]);

  useEffect(() => {
    if (!activeVaultId) return;
    const t = setTimeout(fetchEntries, search ? 300 : 0);
    return () => clearTimeout(t);
  }, [activeVaultId, search, statusFilter, fetchEntries]);

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

  async function handleRecompile() {
    if (!activeVaultId) return;
    try {
      await recompileVaultKMS(activeVaultId);
      toast.info("Recompile queued — document entries will refresh shortly");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to queue recompile");
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-border shrink-0">
        <div className="flex items-center gap-3">
          <Library className="w-5 h-5 text-muted-foreground" />
          <h1 className="text-xl font-semibold">Knowledge Management</h1>
          <VaultSelector />
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={handleRecompile}
            disabled={!activeVaultId}
            title="Recompile document entries for this vault"
          >
            <RefreshCw className="w-4 h-4 mr-1" />
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
        </div>
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
          <SelectTrigger className="w-40">
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
            <p className="text-sm text-muted-foreground text-center py-12">
              Select a vault to view its knowledge entries.
            </p>
          ) : loading ? (
            <p className="text-sm text-muted-foreground py-8">Loading…</p>
          ) : error ? (
            <p className="text-sm text-destructive py-8">{error}</p>
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
