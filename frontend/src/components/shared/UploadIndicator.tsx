import { useUploadStore } from "@/stores/useUploadStore";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import { X, Upload } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";

export function UploadIndicator() {
  const { uploads, clearCompleted } = useUploadStore();
  const [isExpanded, setIsExpanded] = useState(false);

  const activeUploads = uploads.filter(
    (u) =>
      u.status === "pending" ||
      u.status === "uploading" ||
      u.status === "processing" ||
      u.status === "indexing"
  );
  const completedCount = uploads.filter((u) => u.status === "indexed").length;
  const hasCompleted = uploads.some(
    (u) => u.status === "indexed" || u.status === "error" || u.status === "cancelled"
  );

  // Only show if there are active uploads or completed uploads that haven't been cleared
  if (uploads.length === 0 || (activeUploads.length === 0 && !hasCompleted)) {
    return null;
  }

  const currentUpload = uploads.find((u) => u.status === "uploading");
  const pendingCount = activeUploads.length;

  return (
    <div className="fixed bottom-20 right-4 z-50 md:bottom-4 md:right-4 max-w-sm w-full">
      <div
        className={cn(
          "bg-card border rounded-sm shadow-lg overflow-hidden transition-all",
          isExpanded ? "max-h-96" : "max-h-14"
        )}
      >
        {/* Header - always visible */}
        <div
          role="button"
          tabIndex={0}
          className="w-full flex items-center justify-between p-3 cursor-pointer hover:bg-muted/50"
          onClick={() => setIsExpanded(!isExpanded)}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setIsExpanded(!isExpanded); } }}
          aria-expanded={isExpanded}
          aria-label="Toggle upload details"
        >
          <div className="flex items-center gap-2" aria-live="polite">
            <Upload className="w-4 h-4 text-primary" />
            <span className="text-sm font-medium">
              {currentUpload
                ? `Uploading ${currentUpload.file.name}...`
                : pendingCount > 0
                ? `${pendingCount} file(s) in queue`
                : `${completedCount} upload(s) completed`}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {hasCompleted && (
              <Button
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-xs"
                onClick={(e) => {
                  e.stopPropagation();
                  clearCompleted();
                }}
              >
                Clear
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6"
              onClick={(e) => {
                e.stopPropagation();
                setIsExpanded(!isExpanded);
              }}
              aria-label={isExpanded ? "Collapse upload details" : "Expand upload details"}
            >
              <X className="w-3 h-3" aria-hidden="true" />
            </Button>
          </div>
        </div>

        {/* Progress bar - always visible if uploading */}
        {currentUpload && (
          <div className="px-3 pb-2">
            <Progress value={currentUpload.uploadProgress} className="h-1" aria-label="Upload progress" />
          </div>
        )}

        {/* Expanded list */}
        {isExpanded && (
          <div className="px-3 pb-3 space-y-2 max-h-64 overflow-y-auto">
            {uploads.slice(0, 10).map((upload) => (
              <div key={upload.id} className="flex items-center justify-between text-xs">
                <span className="truncate max-w-[200px]" title={upload.file.name}>
                  {upload.file.name}
                </span>
                <span
                  className={cn(
                    "shrink-0",
                    upload.status === "indexed" && "text-emerald-600",
                    upload.status === "error" && "text-destructive",
                    upload.status === "cancelled" && "text-muted-foreground",
                    (upload.status === "indexing" || upload.status === "processing") && "text-blue-600"
                  )}
                >
                  {upload.status === "indexed" && "Done"}
                  {upload.status === "error" && "Error"}
                  {upload.status === "cancelled" && "Cancelled"}
                  {upload.status === "pending" && "Pending"}
                  {upload.status === "uploading" &&
                    `${upload.uploadProgress > 0 ? upload.uploadProgress : 0}%`}
                  {(upload.status === "indexing" || upload.status === "processing") &&
                    (upload.phaseLabel ?? "Processing…")}
                </span>
              </div>
            ))}
            {uploads.length > 10 && (
              <p className="text-xs text-muted-foreground text-center">
                +{uploads.length - 10} more files
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
