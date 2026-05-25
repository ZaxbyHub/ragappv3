import { useRef } from "react";
import { Link } from "react-router-dom";
import { useVirtualizer } from "@tanstack/react-virtual";
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

  const tableVirtualizer = useVirtualizer({
    count: documents.length,
    getScrollElement: () => tableScrollRef.current,
    estimateSize: () => 72,
    overscan: 5,
  });

  return (
    <Card className="hidden sm:block">
      <CardContent className="p-0">
        <div ref={tableScrollRef} className="overflow-auto" style={{ maxHeight: "70vh" }}>
          <table className="w-full" style={{ tableLayout: "fixed" }}>
            <caption className="sr-only">Documents List</caption>
            <thead style={{ position: "sticky", top: 0, zIndex: 10 }}>
              <tr role="row" className="border-b bg-muted" style={{ display: "flex" }}>
                <th scope="col" className="text-left p-4 font-medium flex-none w-12">
                  <Checkbox
                    checked={selectedIds.size > 0 && selectedIds.size === documents.length}
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
                  <div
                    className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize hover:bg-border transition-colors"
                    onMouseDown={onResizeMouseDown}
                    role="separator"
                    aria-orientation="vertical"
                    aria-label="Resize filename column"
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
              role="rowgroup"
              style={{ height: `${tableVirtualizer.getTotalSize()}px`, position: "relative" }}
            >
              {tableVirtualizer.getVirtualItems().map((virtualItem) => {
                const doc = documents[virtualItem.index];
                const docId = String(doc.id);
                const isSelected = Boolean(selectedIds.has(docId));
                return (
                  <tr
                    key={docId}
                    role="row"
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
                        <FileIcon filename={doc.filename} className="w-4 h-4 flex-shrink-0" />
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
                      <StatusBadge status={doc.metadata?.status as string} />
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
                        if (!ws || ws.wiki_status === "not_compiled" || ws.wiki_status === "skipped") {
                          return (
                            <button
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
                            ? "text-green-600"
                            : ws.wiki_status === "failed"
                              ? "text-destructive"
                              : "text-blue-500";
                        const label =
                          ws.wiki_status === "compiled"
                            ? `${ws.pages_count}p / ${ws.claims_count}c`
                            : ws.wiki_status === "compiling"
                              ? "Compiling…"
                              : "Failed";
                        return (
                          <span
                            className={`text-xs font-mono cursor-pointer ${color}`}
                            onClick={() => onCompileDocument(docId)}
                            title={`Wiki: ${ws.wiki_status} — ${ws.pages_count} pages, ${ws.claims_count} claims, ${ws.lint_count} lint issues. Click to recompile.`}
                          >
                            {label}
                          </span>
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
