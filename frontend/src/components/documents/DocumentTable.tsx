import { useRef } from "react";
import { Link } from "react-router-dom";
import { useVirtualizer, type VirtualItem } from "@tanstack/react-virtual";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Trash2, Download, ArrowUp, ArrowDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { FileIcon } from "@/lib/fileIcon";
import { formatFileSize, formatDate } from "@/lib/formatters";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { DocumentProgressCell, documentProgress } from "./documentProgress";
import type {
  Document,
  DocumentWikiStatus,
  DocumentSortBy,
  SortOrder,
} from "@/lib/api";

interface DocumentTableProps {
  documents: Document[];
  selectedIds: Set<string>;
  canMutateDocuments: boolean;
  filenameColWidth: number;
  onResizeMouseDown: (e: React.MouseEvent<HTMLDivElement>) => void;
  onResizeKeyDown: (e: React.KeyboardEvent<HTMLDivElement>) => void;
  onResizeTouchStart: (e: React.TouchEvent<HTMLDivElement>) => void;
  onSelectAll: (checked: boolean | "indeterminate") => void;
  onSelectOne: (docId: string, checked: boolean) => void;
  wikiStatusMap: Record<string, DocumentWikiStatus>;
  compilingDocIds: Set<string>;
  onCompileDocument: (docId: string) => void;
  onDownload: (doc: Document) => void;
  onDelete: (docId: string) => void;
  sortBy: DocumentSortBy;
  sortOrder: SortOrder;
  onSort: (column: DocumentSortBy) => void;
}

function SortHeader({
  label,
  column,
  sortBy,
  sortOrder,
  onSort,
  className,
}: {
  label: string;
  column: DocumentSortBy;
  sortBy: DocumentSortBy;
  sortOrder: SortOrder;
  onSort: (column: DocumentSortBy) => void;
  className?: string;
}) {
  const active = sortBy === column;
  return (
    <th scope="col" className={cn("text-left p-4 font-medium flex-none", className)}>
      <button
        type="button"
        className="inline-flex items-center gap-1 hover:text-foreground"
        onClick={() => onSort(column)}
        aria-label={`Sort by ${label}`}
      >
        {label}
        {active &&
          (sortOrder === "asc" ? (
            <ArrowUp className="w-3 h-3" aria-hidden="true" />
          ) : (
            <ArrowDown className="w-3 h-3" aria-hidden="true" />
          ))}
      </button>
    </th>
  );
}

export function DocumentTable({
  documents,
  selectedIds,
  canMutateDocuments,
  filenameColWidth,
  onResizeMouseDown,
  onResizeKeyDown,
  onResizeTouchStart,
  onSelectAll,
  onSelectOne,
  wikiStatusMap,
  compilingDocIds,
  onCompileDocument,
  onDownload,
  onDelete,
  sortBy,
  sortOrder,
  onSort,
}: DocumentTableProps) {
  const tableScrollRef = useRef<HTMLDivElement>(null);

  // Header checkbox state derives from the selection INTERSECTED with the
  // visible rows — never from size coincidence (selection.size ===
  // documents.length with disjoint ids must read unchecked). Checked = every
  // visible row selected; indeterminate = some but not all.
  const selectedVisibleCount = documents.reduce(
    (count, doc) => count + (selectedIds.has(String(doc.id)) ? 1 : 0),
    0
  );
  const allVisibleSelected =
    documents.length > 0 && selectedVisibleCount === documents.length;
  const headerChecked: boolean | "indeterminate" = allVisibleSelected
    ? true
    : selectedVisibleCount > 0
      ? "indeterminate"
      : false;

  const tableVirtualizer = useVirtualizer({
    count: documents.length,
    getScrollElement: () => tableScrollRef.current,
    estimateSize: () => 72,
    overscan: 5,
  });

  // Unmeasured-container fallback: until the virtualizer has measured its
  // scroll container (first ResizeObserver tick) — or inside any 0-height
  // container — the visible window is empty and the table would render ZERO
  // rows even though documents exist. Render the fetched window directly so
  // the table is never blank while unmeasured; the virtualizer takes over as
  // soon as a real rect arrives. (72 matches estimateSize above.)
  const ESTIMATED_ROW_HEIGHT = 72;
  const virtualItems = tableVirtualizer.getVirtualItems();
  const rowItems: VirtualItem[] =
    virtualItems.length > 0 || documents.length === 0
      ? virtualItems
      : documents.map(
          (_, index): VirtualItem => ({
            index,
            key: index,
            start: index * ESTIMATED_ROW_HEIGHT,
            size: ESTIMATED_ROW_HEIGHT,
            end: (index + 1) * ESTIMATED_ROW_HEIGHT,
            lane: 0,
          })
        );

  return (
    <Card className="hidden sm:block">
      <CardContent className="p-0">
        <div ref={tableScrollRef} className="overflow-auto" style={{ maxHeight: "70vh" }}>
          <table
            className="w-full"
            style={{ tableLayout: "fixed" }}
            aria-rowcount={documents.length + 1}
            aria-colcount={10}
          >
            <caption className="sr-only">Documents List</caption>
            <thead style={{ position: "sticky", top: 0, zIndex: 10 }}>
              <tr role="row" className="border-b bg-muted" style={{ display: "flex" }}>
                <th scope="col" className="text-left p-4 font-medium flex-none w-12">
                  <Checkbox
                    checked={headerChecked}
                    onCheckedChange={onSelectAll}
                    disabled={!canMutateDocuments}
                    aria-label="Select all documents"
                  />
                </th>
                <th
                  scope="col"
                  className="text-left p-4 font-medium relative flex-none"
                  style={{ width: filenameColWidth, flexShrink: 0 }}
                >
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 hover:text-foreground"
                    onClick={() => onSort("file_name")}
                    aria-label="Sort by Filename"
                  >
                    Filename
                    {sortBy === "file_name" &&
                      (sortOrder === "asc" ? (
                        <ArrowUp className="w-3 h-3" aria-hidden="true" />
                      ) : (
                        <ArrowDown className="w-3 h-3" aria-hidden="true" />
                      ))}
                  </button>
                  {/* eslint-disable-next-line jsx-a11y-x/no-noninteractive-element-interactions -- APG window-splitter pattern; keyboard + touch parity provided (WCAG 2.1.1/2.5.1). */}
                  <div
                    className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize hover:bg-border transition-colors touch-none"
                    onMouseDown={onResizeMouseDown}
                    onKeyDown={onResizeKeyDown}
                    onTouchStart={onResizeTouchStart}
                    role="separator"
                    aria-orientation="vertical"
                    aria-label="Resize filename column"
                    aria-valuemin={120}
                    aria-valuemax={600}
                    aria-valuenow={filenameColWidth}
                    tabIndex={0}
                  />
                </th>
                <SortHeader
                  label="Status"
                  column="status"
                  sortBy={sortBy}
                  sortOrder={sortOrder}
                  onSort={onSort}
                  className="w-[120px]"
                />
                <th scope="col" className="text-left p-4 font-medium flex-none w-[180px]">Progress</th>
                <th scope="col" className="text-left p-4 font-medium flex-none w-20">Chunks</th>
                <th scope="col" className="text-left p-4 font-medium flex-none w-[120px]">Wiki</th>
                <th scope="col" className="text-left p-4 font-medium flex-none w-[160px]">Tags</th>
                <SortHeader
                  label="Size"
                  column="file_size"
                  sortBy={sortBy}
                  sortOrder={sortOrder}
                  onSort={onSort}
                  className="w-[100px]"
                />
                <SortHeader
                  label="Uploaded"
                  column="created_at"
                  sortBy={sortBy}
                  sortOrder={sortOrder}
                  onSort={onSort}
                  className="w-[140px]"
                />
                <th scope="col" className="text-right p-4 font-medium flex-none w-[110px]">Actions</th>
              </tr>
            </thead>
            <tbody
              style={{ height: `${tableVirtualizer.getTotalSize()}px`, position: "relative" }}
            >
              {rowItems.map((virtualItem) => {
                const doc = documents[virtualItem.index];
                const docId = String(doc.id);
                const isSelected = Boolean(selectedIds.has(docId));
                return (
                  <tr
                    key={docId}
                    role="row"
                    aria-rowindex={virtualItem.index + 2}
                    data-index={virtualItem.index}
                    ref={tableVirtualizer.measureElement}
                    style={{
                      position: "absolute",
                      top: virtualItem.start,
                      left: 0,
                      width: "100%",
                      display: "flex",
                    }}
                    className={`border-b hover:bg-muted/50 ${isSelected ? "bg-muted/30" : ""}`}
                  >
                    <td className="p-4 flex-none w-12">
                      <Checkbox
                        checked={isSelected}
                        onCheckedChange={(checked) => onSelectOne(docId, !!checked)}
                        disabled={!canMutateDocuments}
                        aria-label={`Select ${doc.filename}`}
                      />
                    </td>
                    <td className="p-4 flex-none" style={{ width: filenameColWidth, flexShrink: 0 }}>
                      <div className="flex items-center gap-2">
                        <FileIcon filename={doc.filename} className="w-4 h-4 shrink-0" />
                        <Link
                          to={`/documents/${docId}`}
                          className="font-medium truncate max-w-full hover:underline"
                          title={doc.filename}
                        >
                          {doc.filename}
                        </Link>
                      </div>
                    </td>
                    <td
                      className="p-4 flex-none w-[120px]"
                      title={documentProgress(doc).errorMessage ?? undefined}
                    >
                      <StatusBadge
                        status={doc.metadata?.status as string}
                        chunksFailed={Number(doc.metadata?.chunks_failed ?? 0)}
                      />
                    </td>
                    <td className="p-4 flex-none w-[180px]">
                      <DocumentProgressCell doc={doc} />
                    </td>
                    <td className="p-4 flex-none w-20">
                      {(() => {
                        const count = Number(doc.metadata?.chunk_count ?? 0);
                        const status = doc.metadata?.status;
                        const isIndexed = status === "indexed";
                        const isFailed = status === "error" || status === "failed";
                        return (
                          <span
                            title={
                              isFailed
                                ? `${count} chunks · indexing failed`
                                : isIndexed
                                  ? `${count} chunks indexed`
                                  : count > 0
                                    ? `${count} chunks · indexing in progress`
                                    : "Awaiting chunking"
                            }
                            className={cn(
                              isFailed && "text-destructive",
                              !isIndexed && !isFailed && count > 0 && "text-muted-foreground italic"
                            )}
                          >
                            {count}
                          </span>
                        );
                      })()}
                    </td>
                    <td className="p-4 flex-none w-[120px]">
                      {(() => {
                        const ws = wikiStatusMap[docId];
                        const isCompiling =
                          compilingDocIds.has(docId) || ws?.wiki_status === "compiling";
                        // AC29 (#515): a document the compiler SKIPPED (no
                        // extractable knowledge) gets a distinct label — it is
                        // NOT uncompiled, so it must not offer the generic
                        // Compile affordance that invites a pointless retry.
                        if (ws?.wiki_status === "skipped") {
                          return (
                            <span
                              className="text-xs text-muted-foreground italic"
                              title="Wiki compile skipped — no extractable knowledge found in this document."
                            >
                              Skipped — no extractable knowledge
                            </span>
                          );
                        }
                        if (!ws || ws.wiki_status === "not_compiled") {
                          return (
                            <button
                              type="button"
                              className="text-xs text-muted-foreground hover:text-foreground underline"
                              onClick={() => onCompileDocument(docId)}
                              disabled={isCompiling}
                              title="Compile wiki for this document"
                            >
                              {isCompiling ? "Queuing…" : "Compile"}
                            </button>
                          );
                        }
                        const color =
                          ws.wiki_status === "compiled"
                            ? "text-success"
                            : ws.wiki_status === "failed"
                              ? "text-destructive"
                              : "text-warning";
                        const label =
                          ws.wiki_status === "compiled"
                            ? `${ws.pages_count}p / ${ws.claims_count}c`
                            : ws.wiki_status === "compiling"
                              ? "Compiling…"
                              : "Failed";
                        return (
                          <button
                            type="button"
                            className={`text-xs font-mono cursor-pointer bg-transparent border-0 p-0 ${color}`}
                            onClick={() => onCompileDocument(docId)}
                            title={`Wiki: ${ws.wiki_status} — ${ws.pages_count} pages, ${ws.claims_count} claims, ${ws.lint_count} lint issues. Click to recompile.`}
                            aria-label={`Recompile wiki for ${doc.filename}. Status: ${ws.wiki_status}.`}
                          >
                            {label}
                          </button>
                        );
                      })()}
                    </td>
                    <td className="p-4 flex-none w-[160px]">
                      <div className="flex flex-wrap gap-1">
                        {(doc.tags ?? []).slice(0, 3).map((tag) => (
                          <Badge key={tag.id} variant="outline" className="text-xs">
                            {tag.name}
                          </Badge>
                        ))}
                        {(doc.tags?.length ?? 0) > 3 && (
                          <Badge variant="secondary" className="text-xs">
                            +{(doc.tags?.length ?? 0) - 3}
                          </Badge>
                        )}
                      </div>
                    </td>
                    <td className="p-4 flex-none w-[100px]">{formatFileSize(doc.size)}</td>
                    <td className="p-4 flex-none w-[140px] text-muted-foreground">
                      {formatDate(doc.created_at)}
                    </td>
                    <td className="p-4 flex-none w-[110px] text-right">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="min-w-[44px] min-h-[44px]"
                        onClick={() => onDownload(doc)}
                        aria-label="Download document"
                      >
                        <Download className="w-4 h-4" aria-hidden="true" />
                      </Button>
                      {canMutateDocuments && (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="min-w-[44px] min-h-[44px]"
                          onClick={() => onDelete(docId)}
                          aria-label="Delete document"
                        >
                          <Trash2 className="w-4 h-4 text-destructive" aria-hidden="true" />
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
