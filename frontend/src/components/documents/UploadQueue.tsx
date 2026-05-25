import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { X, RotateCcw } from "lucide-react";
import type { UploadFile } from "@/stores/useUploadStore";

interface UploadQueueProps {
  uploads: UploadFile[];
  onClearCompleted: () => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onRemove: (id: string) => void;
}

export function UploadQueue({
  uploads,
  onClearCompleted,
  onCancel,
  onRetry,
  onRemove,
}: UploadQueueProps) {
  if (uploads.length === 0) return null;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle className="text-sm">Upload Queue</CardTitle>
          <CardDescription>
            {uploads.filter((u) => u.status === "pending").length} pending,{" "}
            {uploads.filter((u) => u.status === "uploading").length} uploading,{" "}
            {
              uploads.filter(
                (u) => u.status === "indexing" || u.status === "processing"
              ).length
            }{" "}
            processing,{" "}
            {uploads.filter((u) => u.status === "indexed").length} done
          </CardDescription>
        </div>
        {uploads.some(
          (u) => u.status === "indexed" || u.status === "error" || u.status === "cancelled"
        ) && (
          <Button variant="ghost" size="sm" onClick={onClearCompleted}>
            Clear Completed
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {uploads.map((upload) => {
          const isUploading = upload.status === "uploading";
          const isProcessing = upload.status === "processing" || upload.status === "indexing";
          const wikiActive = upload.wikiStatus === "pending" || upload.wikiStatus === "running";
          const wikiTerminal =
            upload.wikiStatus === "completed" ||
            upload.wikiStatus === "failed" ||
            upload.wikiStatus === "cancelled";
          const elapsedSec =
            upload.elapsedSeconds != null
              ? Math.max(0, Math.round(upload.elapsedSeconds))
              : upload.startedAt
                ? Math.round((Date.now() - upload.startedAt) / 1000)
                : null;
          const phaseLabel =
            upload.phaseLabel ??
            (isUploading
              ? "Uploading"
              : isProcessing
                ? "Processing"
                : upload.status === "indexed"
                  ? wikiActive
                    ? "Ready for search · Wiki building"
                    : wikiTerminal && upload.wikiStatus === "completed"
                      ? "Ready with wiki"
                      : "Ready"
                  : upload.status === "error"
                    ? "Error"
                    : upload.status === "cancelled"
                      ? "Cancelled"
                      : "Pending");
          const unitsText =
            upload.processedUnits != null && upload.totalUnits != null
              ? `${upload.processedUnits.toLocaleString()} / ${upload.totalUnits.toLocaleString()} ${
                  upload.unitLabel ?? ""
                }`.trim()
              : null;
          return (
            <div key={upload.id} className="space-y-2 rounded-md border border-border/40 p-3">
              <div className="flex justify-between items-center text-sm">
                <span className="truncate max-w-[250px] font-medium" title={upload.file.name}>
                  {upload.file.name}
                </span>
                <div className="flex items-center gap-2">
                  <span
                    className={
                      upload.status === "error"
                        ? "text-destructive text-xs"
                        : upload.status === "indexed"
                          ? "text-success text-xs"
                          : "text-muted-foreground text-xs"
                    }
                    title={upload.error ?? upload.phaseMessage ?? undefined}
                  >
                    {phaseLabel}
                  </span>
                  {elapsedSec != null && upload.status !== "pending" && (
                    <span className="text-muted-foreground text-xs tabular-nums">
                      {Math.floor(elapsedSec / 60).toString().padStart(2, "0")}:
                      {(elapsedSec % 60).toString().padStart(2, "0")}
                    </span>
                  )}
                  {upload.status === "pending" && (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      onClick={() => onCancel(upload.id)}
                      aria-label={`Cancel upload for ${upload.file.name}`}
                    >
                      <X className="w-3 h-3" />
                    </Button>
                  )}
                  {upload.status === "error" && (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      onClick={() => onRetry(upload.id)}
                      aria-label={`Retry upload for ${upload.file.name}`}
                    >
                      <RotateCcw className="w-3 h-3" />
                    </Button>
                  )}
                  {(upload.status === "indexed" ||
                    upload.status === "cancelled" ||
                    upload.status === "error") && (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      onClick={() => onRemove(upload.id)}
                      aria-label={`Remove ${upload.file.name} from queue`}
                    >
                      <X className="w-3 h-3" />
                    </Button>
                  )}
                </div>
              </div>

              {/* Upload progress (network) — only while uploading */}
              {isUploading && (
                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-muted-foreground">
                    <span>Upload</span>
                    <span className="tabular-nums">
                      {upload.uploadProgress > 0 ? `${upload.uploadProgress}%` : "Starting..."}
                    </span>
                  </div>
                  <Progress
                    value={upload.uploadProgress}
                    className="h-1.5"
                    aria-label="Upload progress"
                  />
                </div>
              )}

              {/* Server-side processing — numeric or indeterminate bar */}
              {isProcessing && (
                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-muted-foreground">
                    <span>
                      {upload.phaseMessage ?? phaseLabel}
                      {unitsText ? ` · ${unitsText}` : ""}
                    </span>
                    {upload.processingProgress != null && (
                      <span className="tabular-nums">
                        {Math.round(upload.processingProgress)}%
                      </span>
                    )}
                  </div>
                  <Progress
                    value={upload.processingProgress ?? undefined}
                    className="h-1.5"
                    aria-label="Indexing progress"
                  />
                  {upload.longRunning && (
                    <div className="text-xs text-muted-foreground italic">
                      Still working — large files can take many minutes. You can leave this page;
                      processing continues in the background.
                    </div>
                  )}
                </div>
              )}

              {/* Wiki compile — separate from indexing */}
              {(wikiActive || wikiTerminal) && (
                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-muted-foreground">
                    <span>
                      Wiki:{" "}
                      {upload.wikiStatus === "running" ? "compiling" : upload.wikiStatus ?? "waiting"}
                    </span>
                    {upload.wikiProgress != null && (
                      <span className="tabular-nums">{Math.round(upload.wikiProgress)}%</span>
                    )}
                  </div>
                  {upload.wikiStatus === "running" && (
                    <Progress
                      value={upload.wikiProgress ?? undefined}
                      className="h-1.5"
                      aria-label="Wiki compile progress"
                    />
                  )}
                </div>
              )}

              {upload.status === "error" && upload.error && (
                <div className="text-xs text-destructive">{upload.error}</div>
              )}
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
