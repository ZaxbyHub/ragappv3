import { useState, useEffect, useCallback } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Brain, Plus, Search, Trash2, Pencil, Loader2, BookOpen } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";
import { toast } from "sonner";
import { useVaultStore } from "@/stores/useVaultStore";
import { VaultSelector } from "@/components/vault/VaultSelector";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import { useMemorySearch } from "@/hooks/useMemorySearch";
import { useMemoryCrud, getCategoryFromMetadata, getTagsFromMetadata, getSourceFromMetadata, MAX_MEMORY_CONTENT_LENGTH } from "@/hooks/useMemoryCrud";
import { updateMemory, promoteMemoryToWiki, getMemoryWikiStatus, type MemoryResult, type MemoryWikiStatus } from "@/lib/api";

export default function MemoryPage() {
  const { activeVaultId } = useVaultStore();

  if (activeVaultId === null) {
    return (
      <>
        <PageTitleHeader title="Memories" />
        <div className="p-8">
          <EmptyState
            icon={Brain}
            title="Select a vault"
            description="Choose a vault from the vault selector to view its memories."
          />
        </div>
      </>
    );
  }

  return <MemoryPageContent activeVaultId={activeVaultId} />;
}

function MemoryPageContent({ activeVaultId }: { activeVaultId: number }) {
  const { memories, searchQuery, setSearchQuery, loading, handleSearch } = useMemorySearch(activeVaultId);

  const {
    isAddDialogOpen,
    setIsAddDialogOpen,
    newMemory,
    setNewMemory,
    isSubmitting,
    isDeleting,
    contentError,
    handleContentChange,
    handleAddMemory,
    handleKeyDown,
    handleDeleteMemory,
  } = useMemoryCrud(activeVaultId, handleSearch);

  // Edit state
  const [editTarget, setEditTarget] = useState<MemoryResult | null>(null);
  const [editContent, setEditContent] = useState("");
  const [editCategory, setEditCategory] = useState("");
  const [editTags, setEditTags] = useState("");
  const [editSource, setEditSource] = useState("");
  const [isSavingEdit, setIsSavingEdit] = useState(false);

  // Delete-confirm dialog state
  const [deleteTarget, setDeleteTarget] = useState<MemoryResult | null>(null);

  // Promote-to-wiki state
  const [promotingId, setPromotingId] = useState<string | null>(null);

  // Wiki status per memory
  const [wikiStatusMap, setWikiStatusMap] = useState<Record<string, MemoryWikiStatus>>({});

  const fetchWikiStatuses = useCallback(async (mems: MemoryResult[]) => {
    if (!activeVaultId || !mems.length) return;
    const results = await Promise.allSettled(
      mems.map((m) => getMemoryWikiStatus(parseInt(m.id, 10), activeVaultId))
    );
    setWikiStatusMap((prev) => {
      const next = { ...prev };
      mems.forEach((m, i) => {
        const r = results[i];
        if (r.status === "fulfilled") next[m.id] = r.value;
      });
      return next;
    });
  }, [activeVaultId]);

  useEffect(() => {
    if (memories && memories.length > 0) fetchWikiStatuses(memories);
  }, [memories, fetchWikiStatuses]);

  function openEdit(memory: MemoryResult) {
    setEditTarget(memory);
    setEditContent(memory.content ?? "");
    setEditCategory(getCategoryFromMetadata(memory.metadata) === "Uncategorized" ? "" : getCategoryFromMetadata(memory.metadata));
    setEditTags(getTagsFromMetadata(memory.metadata).join(", "));
    setEditSource(getSourceFromMetadata(memory.metadata));
  }

  function closeEdit() {
    setEditTarget(null);
  }

  async function handleSaveEdit() {
    if (!editTarget) return;
    if (!editContent.trim()) {
      toast.error("Content cannot be empty");
      return;
    }
    setIsSavingEdit(true);
    try {
      await updateMemory(editTarget.id, {
        content: editContent.trim(),
        category: editCategory.trim() || undefined,
        tags: editTags.trim() || undefined,
        source: editSource.trim() || undefined,
      });
      toast.success("Memory updated");
      closeEdit();
      await handleSearch();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to update memory");
    } finally {
      setIsSavingEdit(false);
    }
  }

  function openDeleteDialog(memory: MemoryResult) {
    setDeleteTarget(memory);
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    await handleDeleteMemory(deleteTarget.id);
    setDeleteTarget(null);
  }

  async function handlePromoteToWiki(memory: MemoryResult) {
    setPromotingId(memory.id);
    try {
      const result = await promoteMemoryToWiki({
        memory_id: parseInt(memory.id, 10),
        vault_id: activeVaultId,
      });
      toast.success(
        `Promoted to wiki: "${result.page.title}" — open Wiki to view`,
        { duration: 6000 }
      );
      // Refresh wiki status for this memory
      setTimeout(() => fetchWikiStatuses([memory]), 1500);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Promote failed");
    } finally {
      setPromotingId(null);
    }
  }

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <PageTitleHeader
          title="Memory"
          description="View and manage AI memory and context"
        />
        <div className="flex items-center gap-2">
          <VaultSelector />
          <Button onClick={() => setIsAddDialogOpen(true)}>
            <Plus className="w-4 h-4 mr-2" />
            Add Memory
          </Button>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            placeholder="Search memories..."
            className="pl-10"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          />
        </div>
        <Button variant="outline" size="icon" onClick={handleSearch} disabled={loading}>
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
        </Button>
        <Badge variant="secondary">{memories?.length || 0} {searchQuery ? "results" : "memories"}</Badge>
      </div>

      {/* Add Memory Form */}
      {isAddDialogOpen && (
        <Card>
          <CardHeader>
            <CardTitle>Add New Memory</CardTitle>
            <CardDescription>Create a new memory entry for the AI</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="memory-content">Content *</Label>
              <Textarea
                id="memory-content"
                placeholder="Enter memory content..."
                className={`min-h-[100px] ${contentError ? "border-destructive focus-visible:ring-destructive" : ""}`}
                value={newMemory.content}
                onChange={handleContentChange}
                onKeyDown={handleKeyDown}
              />
              {contentError && <span className="text-xs text-destructive">{contentError}</span>}
              <div className="flex justify-end">
                <span className={`text-xs ${newMemory.content.length > MAX_MEMORY_CONTENT_LENGTH ? "text-destructive" : "text-muted-foreground"}`}>
                  {newMemory.content.length}/{MAX_MEMORY_CONTENT_LENGTH}
                </span>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="memory-category">Category</Label>
                <Input
                  id="memory-category"
                  placeholder="e.g., facts, preferences"
                  value={newMemory.category}
                  onChange={(e) => setNewMemory({ ...newMemory, category: e.target.value })}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="memory-source">Source</Label>
                <Input
                  id="memory-source"
                  placeholder="e.g., user input, document"
                  value={newMemory.source}
                  onChange={(e) => setNewMemory({ ...newMemory, source: e.target.value })}
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="memory-tags">Tags</Label>
              <Input
                id="memory-tags"
                placeholder="Enter tags separated by commas..."
                value={newMemory.tags}
                onChange={(e) => setNewMemory({ ...newMemory, tags: e.target.value })}
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setIsAddDialogOpen(false)}>Cancel</Button>
              <Button onClick={handleAddMemory} disabled={isSubmitting || !newMemory.content.trim()}>
                {isSubmitting ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Plus className="w-4 h-4 mr-2" />}
                Add Memory
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Memory List — normal document flow, no virtualization */}
      {loading && (!memories || memories.length === 0) ? (
        <div className="space-y-4">
          {[...Array(4)].map((_, i) => (
            <Card key={i}>
              <CardContent className="p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 space-y-3">
                    <Skeleton className="h-4 w-full" />
                    <Skeleton className="h-4 w-[90%]" />
                    <Skeleton className="h-4 w-[60%]" />
                    <div className="flex flex-wrap items-center gap-2 pt-2">
                      <Skeleton className="h-5 w-[70px]" />
                      <Skeleton className="h-5 w-[50px]" />
                    </div>
                  </div>
                  <Skeleton className="h-8 w-8 shrink-0" />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : !memories || memories.length === 0 ? (
        <EmptyState
          icon={Brain}
          title={searchQuery ? "No memories match your search" : "No memories yet"}
          description={searchQuery ? undefined : "Add some to get started."}
        />
      ) : (
        <div className="space-y-3">
          {memories.map((memory) => (
            <Card key={memory.id}>
              <CardContent className="p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 space-y-2">
                    <p className="text-sm whitespace-pre-wrap">{memory.content}</p>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="outline">{getCategoryFromMetadata(memory.metadata)}</Badge>
                      {getTagsFromMetadata(memory.metadata).map((tag, idx) => (
                        <Badge key={idx} variant="secondary" className="text-xs">{tag}</Badge>
                      ))}
                      {getSourceFromMetadata(memory.metadata) && (
                        <span className="text-xs text-muted-foreground">
                          Source: {getSourceFromMetadata(memory.metadata)}
                        </span>
                      )}
                      {(() => {
                        const ws = wikiStatusMap[memory.id];
                        if (!ws || ws.wiki_status === "not_promoted") return null;
                        const colorMap: Record<string, string> = {
                          promoted: "text-green-600",
                          stale: "text-yellow-600",
                          promoting: "text-blue-500",
                        };
                        const labelMap: Record<string, string> = {
                          promoted: `Wiki: ${ws.active_claims}c / ${ws.linked_pages.length}p`,
                          stale: `Wiki: stale (${ws.stale_claims} stale)`,
                          promoting: "Wiki: promoting…",
                        };
                        return (
                          <span
                            className={`text-xs font-mono ${colorMap[ws.wiki_status] ?? "text-muted-foreground"}`}
                            title={`Wiki status: ${ws.wiki_status} — ${ws.claims_count} claims, ${ws.linked_pages.length} pages`}
                          >
                            {labelMap[ws.wiki_status] ?? ws.wiki_status}
                          </span>
                        );
                      })()}
                    </div>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8"
                      onClick={() => handlePromoteToWiki(memory)}
                      disabled={promotingId === memory.id}
                      aria-label="Promote to Wiki"
                      title="Promote to Wiki"
                    >
                      {promotingId === memory.id ? (
                        <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />
                      ) : (
                        <BookOpen className="w-4 h-4 text-muted-foreground" />
                      )}
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8"
                      onClick={() => openEdit(memory)}
                      aria-label="Edit memory"
                    >
                      <Pencil className="w-4 h-4 text-muted-foreground" />
                    </Button>
                    <Button
                      variant="destructive"
                      size="icon"
                      className="h-8 w-8"
                      onClick={() => openDeleteDialog(memory)}
                      disabled={isDeleting === memory.id}
                      aria-label="Delete memory"
                    >
                      {isDeleting === memory.id ? (
                        <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />
                      ) : (
                        <Trash2 className="w-4 h-4" />
                      )}
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Edit Memory Dialog */}
      <Dialog open={!!editTarget} onOpenChange={(open) => { if (!open) closeEdit(); }}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>Edit Memory</DialogTitle>
            <DialogDescription>Update memory content, category, tags, or source.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label htmlFor="edit-content">Content *</Label>
              <Textarea
                id="edit-content"
                className="min-h-[100px]"
                value={editContent}
                onChange={(e) => setEditContent(e.target.value)}
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="edit-category">Category</Label>
                <Input id="edit-category" value={editCategory} onChange={(e) => setEditCategory(e.target.value)} />
              </div>
              <div className="space-y-2">
                <Label htmlFor="edit-source">Source</Label>
                <Input id="edit-source" value={editSource} onChange={(e) => setEditSource(e.target.value)} />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-tags">Tags (comma-separated)</Label>
              <Input id="edit-tags" value={editTags} onChange={(e) => setEditTags(e.target.value)} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeEdit}>Cancel</Button>
            <Button onClick={handleSaveEdit} disabled={isSavingEdit || !editContent.trim()}>
              {isSavingEdit && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
              Save Changes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirm Dialog */}
      <Dialog open={!!deleteTarget} onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Memory</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this memory? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {deleteTarget && (
            <p className="text-sm text-muted-foreground border rounded-sm p-3 bg-muted line-clamp-3">
              {deleteTarget.content}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>Cancel</Button>
            <Button variant="destructive" onClick={confirmDelete} disabled={isDeleting === deleteTarget?.id}>
              {isDeleting === deleteTarget?.id && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
