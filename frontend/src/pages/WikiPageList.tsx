import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { LoadingSpinner } from "@/components/LoadingSpinner";
import { EmptyState } from "@/components/EmptyState";
import { FileText, Trash2, RefreshCw } from "lucide-react";
import type { WikiPage } from "@/lib/api";
import { bulkWikiPageAction } from "@/lib/api";

export const PAGE_TYPES = [
  { value: "", label: "All" },
  { value: "overview", label: "Overview" },
  { value: "entity", label: "Entities" },
  { value: "system", label: "Systems" },
  { value: "procedure", label: "Procedures" },
  { value: "acronym", label: "Acronyms" },
  { value: "qa", label: "Q&A" },
  { value: "contradiction", label: "Contradictions" },
  { value: "open_question", label: "Open Questions" },
] as const;

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-muted text-muted-foreground",
  verified: "bg-success/10 text-success",
  stale: "bg-warning/10 text-warning",
  needs_review: "bg-primary/10 text-primary",
  archived: "bg-destructive/10 text-destructive",
};

interface WikiPageListProps {
  pages: WikiPage[];
  loading: boolean;
  onSelect: (pageId: number) => void;
  vaultId?: number | null;
  /** Called after a bulk action so the parent can refetch the current view. */
  onRefresh?: () => void;
}

export function WikiPageList({ pages, loading, onSelect, vaultId, onRefresh }: WikiPageListProps) {
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [bulkLoading, setBulkLoading] = useState(false);

  // Clear selection when pages change
  useEffect(() => {
    setSelectedIds([]);
  }, [pages]);

  function toggleSelectAll() {
    if (selectedIds.length === pages.length) {
      setSelectedIds([]);
    } else {
      setSelectedIds(pages.map((p) => p.id));
    }
  }

  async function handleBulkDelete() {
    if (!vaultId || selectedIds.length === 0) return;
    if (!window.confirm(`Delete ${selectedIds.length} selected page(s)?`)) return;
    setBulkLoading(true);
    try {
      await bulkWikiPageAction(vaultId, selectedIds, "delete");
      setSelectedIds([]);
      onRefresh?.();
    } catch (err) {
      // The api interceptor normalizes but does NOT render a toast — surface
      // the failure to the user so a failed bulk-delete is not silent (UI-INT-3).
      toast.error(err instanceof Error ? err.message : "Failed to delete pages");
    } finally {
      setBulkLoading(false);
    }
  }

  async function handleBulkStatusChange(status: string) {
    if (!vaultId || selectedIds.length === 0) return;
    setBulkLoading(true);
    try {
      await bulkWikiPageAction(vaultId, selectedIds, "update", { status });
      setSelectedIds([]);
      onRefresh?.();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to update page status");
    } finally {
      setBulkLoading(false);
    }
  }

  return (
    <div className="flex flex-col gap-2 h-full overflow-y-auto">
      {/* Bulk action bar */}
      {selectedIds.length > 0 && (
        <div className="flex items-center gap-2 px-2 py-2 bg-muted/50 rounded-md border border-border">
          <span className="text-xs font-medium">{selectedIds.length} selected</span>
          <Button
            variant="outline"
            size="sm"
            onClick={handleBulkDelete}
            disabled={bulkLoading}
            className="text-destructive hover:text-destructive text-xs h-7"
          >
            <Trash2 className="w-3 h-3 mr-1" />
            Delete
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => handleBulkStatusChange("draft")}
            disabled={bulkLoading}
            className="text-xs h-7"
          >
            <RefreshCw className="w-3 h-3 mr-1" />
            Set Draft
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => handleBulkStatusChange("archived")}
            disabled={bulkLoading}
            className="text-xs h-7"
          >
            Archive
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setSelectedIds([])}
            className="text-xs h-7 ml-auto"
          >
            Clear
          </Button>
        </div>
      )}

      {loading && <LoadingSpinner label="Loading pages…" />}
      {!loading && pages.length === 0 && (
        <EmptyState
          icon={FileText}
          title="No pages found"
        />
      )}
      {!loading && pages.length > 0 && (
        <div className="flex items-center gap-2 px-1 mb-1">
          <Checkbox
            checked={selectedIds.length === pages.length && pages.length > 0}
            onCheckedChange={toggleSelectAll}
            className="h-3.5 w-3.5"
            aria-label="Select all pages"
          />
          <span className="text-xs text-muted-foreground">Select all</span>
        </div>
      )}
      {pages.map((page) => (
        <Card
          key={page.id}
          className={`cursor-pointer hover:bg-card/60 transition-colors ${selectedIds.includes(page.id) ? "ring-2 ring-primary/50" : ""}`}
          onClick={() => onSelect(page.id)}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => e.key === "Enter" && onSelect(page.id)}
        >
          <CardContent className="py-3 px-4">
            <div className="flex items-start gap-2">
              <Checkbox
                checked={selectedIds.includes(page.id)}
                onCheckedChange={(checked) => {
                  // Stop propagation so the click does not also trigger the
                  // row's onClick (which would open the page).
                  if (!checked) {
                    setSelectedIds((prev) => prev.filter((id) => id !== page.id));
                  } else {
                    setSelectedIds((prev) => [...prev, page.id]);
                  }
                }}
                onClick={(e) => e.stopPropagation()}
                className="h-3.5 w-3.5 mt-1 shrink-0"
                aria-label={`Select ${page.title}`}
              />
              <div className="flex items-start justify-between gap-2 flex-1 min-w-0">
                <div className="flex-1 min-w-0">
                  <p className="font-medium text-sm truncate">{page.title}</p>
                  <p className="text-xs text-muted-foreground truncate">{page.slug}</p>
                  {page.summary && (
                    <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{page.summary}</p>
                  )}
                </div>
                <div className="flex flex-col items-end justify-between gap-2 shrink-0">
                  <Badge variant="outline" className="text-xs capitalize">{page.page_type}</Badge>
                  <span className={`text-xs px-1.5 py-0.5 rounded-full font-medium capitalize ${STATUS_COLORS[page.status] ?? ""}`}>
                    {page.status.replace("_", " ")}
                  </span>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
