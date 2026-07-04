import { ChevronLeft, ChevronRight, MoreHorizontal } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface PaginationProps {
  /** Current page (1-based) */
  page: number;
  /** Items per page */
  limit: number;
  /** Total number of items from backend */
  total: number;
  /** Callback when page changes */
  onPageChange: (page: number) => void;
  /** Callback when page size changes */
  onLimitChange: (limit: number) => void;
  /** Whether data is currently loading */
  isLoading?: boolean;
  /** Optional className for container */
  className?: string;
  /** Item name for results summary (e.g. "users", "results") */
  itemName?: string;
}

const PAGE_SIZES = [10, 20, 50, 100];

export function Pagination({
  page,
  limit,
  total,
  onPageChange,
  onLimitChange,
  isLoading = false,
  className = "",
  itemName = "users",
}: PaginationProps) {
  const totalPages = Math.ceil(total / limit) || 1;
  const startItem = (page - 1) * limit + 1;
  const endItem = Math.min(page * limit, total);

  const handlePrev = () => {
    if (page > 1) onPageChange(page - 1);
  };

  const handleNext = () => {
    if (page < totalPages) onPageChange(page + 1);
  };

  const goToPage = (targetPage: number) => {
    if (targetPage >= 1 && targetPage <= totalPages) {
      onPageChange(targetPage);
    }
  };

  // Generate page numbers to display (smart truncation)
  const getPageNumbers = (): (number | string)[] => {
    const pages: (number | string)[] = [];
    const maxVisible = 5;

    if (totalPages <= maxVisible) {
      for (let i = 1; i <= totalPages; i++) pages.push(i);
      return pages;
    }

    pages.push(1);

    if (page > 3) pages.push("ellipsis-start");

    const start = Math.max(2, page - 1);
    const end = Math.min(totalPages - 1, page + 1);

    for (let i = start; i <= end; i++) {
      pages.push(i);
    }

    if (page < totalPages - 2) pages.push("ellipsis-end");

    if (totalPages > 1) pages.push(totalPages);

    return pages;
  };

  return (
    <div
      className={`flex flex-col sm:flex-row items-center justify-between gap-4 px-2 py-4 border-t bg-background ${className}`}
      role="navigation"
      aria-label="Pagination navigation"
    >
      {/* Results summary */}
      <div className="text-sm text-muted-foreground flex items-center gap-2">
        {total > 0 ? (
          <>
            Showing <span className="font-medium text-foreground">{startItem}</span> to{" "}
            <span className="font-medium text-foreground">{endItem}</span> of{" "}
            <span className="font-medium text-foreground">{total}</span> {itemName}
          </>
        ) : (
          `No ${itemName} found`
        )}
      </div>

      <div className="flex items-center gap-3">
        {/* Page size selector */}
        <div className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground whitespace-nowrap">Rows per page</span>
          <Select
            value={limit.toString()}
            onValueChange={(value) => {
              const newLimit = parseInt(value);
              onLimitChange(newLimit);
              onPageChange(1); // reset to first page when changing page size
            }}
            disabled={isLoading}
          >
            <SelectTrigger className="h-8 w-[70px]" aria-label="Select page size">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PAGE_SIZES.map((size) => (
                <SelectItem key={size} value={size.toString()}>
                  {size}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {/* Pagination controls */}
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="icon"
            onClick={handlePrev}
            disabled={page === 1 || isLoading}
            aria-label="Go to previous page"
            className="h-8 w-8"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden="true" />
          </Button>

          {/* Page numbers */}
          <div className="flex items-center gap-1">
            {getPageNumbers().map((item, index) => {
              if (item === "ellipsis-start" || item === "ellipsis-end") {
                return (
                  <Button
                    key={`ellipsis-${index}`}
                    variant="ghost"
                    size="icon"
                    disabled
                    className="h-8 w-8 text-muted-foreground"
                    aria-hidden="true"
                  >
                    <MoreHorizontal className="h-4 w-4" />
                  </Button>
                );
              }

              const pageNum = item as number;
              const isCurrent = pageNum === page;

              return (
                <Button
                  key={pageNum}
                  variant={isCurrent ? "default" : "outline"}
                  size="icon"
                  onClick={() => goToPage(pageNum)}
                  disabled={isLoading}
                  aria-label={`Go to page ${pageNum}`}
                  aria-current={isCurrent ? "page" : undefined}
                  className="h-8 w-8"
                >
                  {pageNum}
                </Button>
              );
            })}
          </div>

          <Button
            variant="outline"
            size="icon"
            onClick={handleNext}
            disabled={page === totalPages || isLoading}
            aria-label="Go to next page"
            className="h-8 w-8"
          >
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      </div>
    </div>
  );
}
