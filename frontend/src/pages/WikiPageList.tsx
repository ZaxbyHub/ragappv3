import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { LoadingSpinner } from "@/components/LoadingSpinner";
import { EmptyState } from "@/components/EmptyState";
import { FileText } from "lucide-react";
import type { WikiPage } from "@/lib/api";

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
  verified: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  stale: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
  needs_review: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  archived: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
};

interface WikiPageListProps {
  pages: WikiPage[];
  loading: boolean;
  onSelect: (pageId: number) => void;
}

export function WikiPageList({ pages, loading, onSelect }: WikiPageListProps) {
  return (
    <div className="flex flex-col gap-2 h-full overflow-y-auto">
        {loading && <LoadingSpinner label="Loading pages…" />}
        {!loading && pages.length === 0 && (
          <EmptyState
            icon={FileText}
            title="No pages found"
          />
        )}
        {pages.map((page) => (
          <Card
            key={page.id}
            className="cursor-pointer hover:bg-card/60 transition-colors"
            onClick={() => onSelect(page.id)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => e.key === "Enter" && onSelect(page.id)}
          >
            <CardContent className="py-3 px-4">
              <div className="flex items-start justify-between gap-2">
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
                    {page.status.replace('_', ' ')}
                  </span>
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
    </div>
  );
}
