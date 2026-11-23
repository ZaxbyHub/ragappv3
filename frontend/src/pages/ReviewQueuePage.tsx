import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { BookCheck } from "lucide-react";

import { EmptyState } from "@/components/shared/EmptyState";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  batchReviewQueue,
  listReviewQueue,
  type ReviewQueueItem,
  type ReviewQueueKind,
} from "@/lib/api";
import { useVaultStore } from "@/stores/useVaultStore";
import { formatDate } from "@/lib/formatters";

const KIND_LABELS: Record<ReviewQueueKind, string> = {
  wiki_page: "Wiki page",
  kms_entry: "Knowledge entry",
};

type KindFilter = "all" | ReviewQueueKind;

/**
 * One review queue across the wiki and the knowledge base (issue #1163).
 * Each item renders beside the source quote it rests on; selection drives
 * an all-or-none batch approve/reject.
 */
export default function ReviewQueuePage() {
  const navigate = useNavigate();
  const activeVaultId = useVaultStore((s) => s.activeVaultId);
  const [items, setItems] = useState<ReviewQueueItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [actionLoading, setActionLoading] = useState(false);

  const refresh = async () => {
    if (activeVaultId == null) return;
    setLoading(true);
    setError(null);
    try {
      const res = await listReviewQueue({ vault_id: activeVaultId });
      setItems(res.items);
    } catch {
      setError("Failed to load the review queue.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    setSelectedKeys(new Set());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeVaultId]);

  const visible = useMemo(
    () =>
      items.filter(
        (i) =>
          (kindFilter === "all" || i.kind === kindFilter) &&
          (statusFilter === "all" || i.status === statusFilter)
      ),
    [items, kindFilter, statusFilter]
  );

  const keyOf = (i: ReviewQueueItem) => `${i.kind}:${i.id}`;
  const toggle = (i: ReviewQueueItem) => {
    setSelectedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(keyOf(i))) next.delete(keyOf(i));
      else next.add(keyOf(i));
      return next;
    });
  };
  const selectedItems = visible.filter((i) => selectedKeys.has(keyOf(i)));

  const runBatch = async (action: "approve" | "reject") => {
    if (activeVaultId == null || selectedItems.length === 0) return;
    setActionLoading(true);
    try {
      const res = await batchReviewQueue(
        activeVaultId,
        selectedItems.map((i) => ({ kind: i.kind, id: i.id })),
        action
      );
      toast.success(
        `Review queue: ${res.updated} item(s) ${action === "approve" ? "approved" : "rejected"}.`
      );
      await refresh();
    } catch {
      toast.error("Review queue batch failed; nothing was changed.");
    } finally {
      setActionLoading(false);
    }
  };

  return (
    <div className="space-y-4">
      <PageTitleHeader
        title="Review queue"
        description={
          "Wiki pages and knowledge entries awaiting review, each beside its source quote."
        }
        before={<BookCheck className="w-5 h-5 text-muted-foreground" aria-hidden="true" />}
        actions={
          <Button variant="ghost" size="sm" onClick={() => void refresh()}>
            Refresh
          </Button>
        }
      />

      <div
        className="flex flex-wrap items-center gap-2"
        role="group"
        aria-label="Review queue filters"
      >
        {(["all", "wiki_page", "kms_entry"] as const).map((k) => (
          <Button
            key={k}
            variant={kindFilter === k ? "default" : "outline"}
            size="sm"
            onClick={() => setKindFilter(k)}
            aria-pressed={kindFilter === k}
          >
            {k === "all" ? "All kinds" : KIND_LABELS[k]}
          </Button>
        ))}
        {["all", "needs_review", "draft"].map((s) => (
          <Button
            key={s}
            variant={statusFilter === s ? "default" : "outline"}
            size="sm"
            onClick={() => setStatusFilter(s)}
            aria-pressed={statusFilter === s}
          >
            {s === "all" ? "All statuses" : s}
          </Button>
        ))}
      </div>

      {selectedItems.length > 0 && (
        <div
          className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/40 p-2"
          role="group"
          aria-label="Review queue batch actions"
        >
          <span className="text-sm font-medium">
            {selectedItems.length} selected
          </span>
          <Button
            size="sm"
            onClick={() => void runBatch("approve")}
            disabled={actionLoading}
          >
            Approve
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => void runBatch("reject")}
            disabled={actionLoading}
          >
            Reject
          </Button>
        </div>
      )}

      {loading ? (
        <p role="status">Loading review queue…</p>
      ) : error ? (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      ) : visible.length === 0 ? (
        <EmptyState
          title="Nothing awaiting review"
          description="Wiki pages and compiled knowledge entries that need review will appear here."
        />
      ) : (
        <ul className="space-y-2" aria-label="Review queue items">
          {visible.map((item) => {
            const selected = selectedKeys.has(keyOf(item));
            return (
              <li
                key={keyOf(item)}
                className="rounded-md border p-3"
                data-testid={`review-queue-item-${item.kind}-${item.id}`}
              >
                <div className="flex items-start gap-3">
                  <Checkbox
                    checked={selected}
                    onCheckedChange={() => toggle(item)}
                    aria-label={`Select ${item.title}`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded bg-secondary px-1.5 py-0.5 text-xs">
                        {KIND_LABELS[item.kind]}
                      </span>
                      <span className="font-medium">{item.title}</span>
                      <span className="text-xs text-muted-foreground">
                        {item.slug}
                      </span>
                      {item.updated_at && (
                        <span className="ml-auto text-xs text-muted-foreground">
                          {formatDate(item.updated_at)}
                        </span>
                      )}
                    </div>
                    {item.source_quote && (
                      <blockquote
                        className="mt-2 border-l-2 pl-2 text-sm italic text-muted-foreground"
                        data-testid={`source-quote-${item.kind}-${item.id}`}
                      >
                        {item.source_quote}
                      </blockquote>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <p className="text-xs text-muted-foreground">
        Approve marks wiki pages verified and knowledge entries published;
        reject archives both. Batch actions update every selected item or
        none.{" "}
        <button
          className="underline"
          onClick={() => navigate("/wiki")}
          type="button"
        >
          Open the wiki
        </button>
      </p>
    </div>
  );
}
