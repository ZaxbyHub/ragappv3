// src/components/shared/DocumentCard.tsx

import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Progress } from "@/components/ui/progress";
import { Trash2, MoreVertical, Download } from "lucide-react";
import { FileIcon } from "@/lib/fileIcon";
import { StatusBadge } from "./StatusBadge";
import { formatFileSize, formatDate } from "@/lib/formatters";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

interface DocumentCardProps {
  /** Document data */
  document: {
    id: string;
    filename: string;
    size?: number;
    created_at?: string;
    error_message?: string | null;
    phase?: string | null;
    phase_message?: string | null;
    progress_percent?: number | null;
    processed_units?: number | null;
    total_units?: number | null;
    unit_label?: string | null;
    metadata?: Record<string, unknown>;
  };
  /** Callback when delete action is triggered */
  onDelete: (id: string) => void;
  /** Callback when download action is triggered */
  onDownload?: (id: string) => void;
  /** Loading state for delete action */
  isDeleting?: boolean;
  /** Whether delete actions should be available for this document */
  canDelete?: boolean;
  /** Selection state for bulk operations */
  isSelected?: boolean;
  /** Callback when selection changes */
  onSelectionChange?: (id: string, selected: boolean) => void;
}

/**
 * Mobile‑friendly card layout for document items.
 * Implements WCAG 2.1 AA requirements:
 * - Touch targets ≥44×44px
 * - Semantic HTML structure
 * - Keyboard‑accessible interactive elements
 * - Sufficient color contrast (via shadcn/ui theme)
 * - Screen‑reader labels for icons
 */
export function DocumentCard({
  document,
  onDelete,
  onDownload,
  isDeleting = false,
  canDelete = true,
  isSelected = false,
  onSelectionChange,
}: DocumentCardProps) {
  const status = document.metadata?.status as string | undefined;
  const chunkCount = document.metadata?.chunk_count as number | undefined;
  const phase =
    document.phase ?? (document.metadata?.phase as string | null | undefined);
  const phaseMessage =
    document.phase_message ??
    (document.metadata?.phase_message as string | null | undefined);
  const errorMessage =
    document.error_message ??
    (document.metadata?.error_message as string | null | undefined);
  const progressPercent =
    document.progress_percent ??
    (document.metadata?.progress_percent as number | null | undefined);
  const processedUnits =
    document.processed_units ??
    (document.metadata?.processed_units as number | null | undefined);
  const totalUnits =
    document.total_units ??
    (document.metadata?.total_units as number | null | undefined);
  const unitLabel =
    document.unit_label ??
    (document.metadata?.unit_label as string | null | undefined);
  const isFailed = status === "error" || status === "failed";
  const isActive = status === "pending" || status === "processing";
  const progressLabel =
    phaseMessage ?? phase ?? (isFailed ? "Failed" : status === "indexed" ? "Complete" : "Waiting");
  const progressTitle = isFailed && errorMessage ? errorMessage : progressLabel;
  const unitsText =
    processedUnits != null && totalUnits != null
      ? `${processedUnits.toLocaleString()} / ${totalUnits.toLocaleString()} ${
          unitLabel ?? ""
        }`.trim()
      : null;

  return (
    <Card
      className={`w-full overflow-hidden border-border hover:border-primary/30 transition-colors ${
        isSelected ? "ring-2 ring-primary ring-offset-2" : ""
      }`}
      role="article"
      aria-label={`Document: ${document.filename}`}
    >
      <CardContent className="p-4">
        {/* Header row: checkbox, icon, filename, actions dropdown */}
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-3 min-w-0 flex-1">
            {/* Checkbox for bulk selection */}
            {onSelectionChange && (
              <Checkbox
                checked={!!isSelected}
                onCheckedChange={(checked) =>
                  onSelectionChange(document.id, !!checked)
                }
                aria-label={`Select ${document.filename}`}
                className="flex-shrink-0"
              />
            )}
            <div
              className="flex-shrink-0 p-2 bg-muted rounded-sm"
              aria-hidden="true"
            >
              <FileIcon filename={document.filename} className="w-5 h-5" />
            </div>
            <div className="min-w-0">
              <h3
                className="font-medium text-foreground truncate"
                title={document.filename}
              >
                {document.filename}
              </h3>
  
            </div>
          </div>

          {/* Actions dropdown (≥44×44px touch target) */}
          {(canDelete || onDownload) && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-11 w-11 flex-shrink-0"
                  aria-label={`Actions for ${document.filename}`}
                  aria-haspopup="menu"
                >
                  <MoreVertical className="w-5 h-5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-48">
                {onDownload && (
                  <DropdownMenuItem
                    onClick={() => onDownload(document.id)}
                    aria-label={`Download ${document.filename}`}
                  >
                    <Download className="w-4 h-4 mr-2" />
                    Download
                  </DropdownMenuItem>
                )}
                {canDelete && (
                  <DropdownMenuItem
                    onClick={() => onDelete(document.id)}
                    disabled={isDeleting}
                    className="text-destructive focus:text-destructive bg-destructive/10 hover:!bg-destructive/20"
                    aria-label={`Delete ${document.filename}`}
                  >
                    <Trash2 className="w-4 h-4 mr-2" />
                    Delete
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>

        {/* Metadata row: status, size, date, chunks */}
        <div className="grid grid-cols-2 gap-3 text-sm">
          <div className="space-y-1">
            <div className="text-muted-foreground">Status</div>
            <div title={isFailed && errorMessage ? errorMessage : undefined}>
              <StatusBadge status={status} />
            </div>
          </div>
          <div className="space-y-1">
            <div className="text-muted-foreground">Size</div>
            <div className="text-foreground">
              {document.size ? formatFileSize(document.size) : "—"}
            </div>
          </div>
          <div className="space-y-1">
            <div className="text-muted-foreground">Uploaded</div>
            <div className="text-foreground">
              {document.created_at ? formatDate(document.created_at) : "—"}
            </div>
          </div>
          <div className="space-y-1">
            <div className="text-muted-foreground">Chunks</div>
            <div className="text-foreground">
              {chunkCount !== undefined ? String(chunkCount) : "—"}
            </div>
          </div>
        </div>

        {(isActive || isFailed || progressPercent != null || phaseMessage || phase) && (
          <div className="mt-4 space-y-1" title={progressTitle}>
            <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
              <span className={isFailed ? "text-destructive" : undefined}>
                {progressLabel}
                {unitsText ? ` - ${unitsText}` : ""}
              </span>
              {progressPercent != null && (
                <span className="tabular-nums">{Math.round(progressPercent)}%</span>
              )}
            </div>
            {!isFailed && (
              <Progress
                value={progressPercent ?? undefined}
                className="h-1.5"
                aria-label={`Processing progress for ${document.filename}`}
              />
            )}
          </div>
        )}

        {/* Standalone delete button (visible on larger mobile screens) */}
        {canDelete && (
          <div className="mt-4 flex sm:hidden">
            <Button
              variant="destructive"
              size="sm"
              className="h-11 w-full"
              onClick={() => onDelete(document.id)}
              disabled={isDeleting}
              aria-label={`Delete ${document.filename}`}
              aria-busy={isDeleting}
            >
              <Trash2 className="w-4 h-4 mr-2" />
            {isDeleting ? "Deleting…" : "Delete Document"}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
