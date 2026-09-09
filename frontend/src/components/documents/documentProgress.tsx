import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import type { Document } from "@/lib/api";

export function documentField<T>(doc: Document, key: string): T | null {
  const directValue = (doc as unknown as Record<string, unknown>)[key];
  if (directValue !== undefined && directValue !== null) {
    return directValue as T;
  }
  const metadataValue = doc.metadata?.[key];
  return metadataValue === undefined || metadataValue === null ? null : (metadataValue as T);
}

export function documentProgress(doc: Document) {
  const status = (doc.metadata?.status as string | undefined) ?? "";
  const phase = documentField<string>(doc, "phase");
  const phaseMessage = documentField<string>(doc, "phase_message");
  const errorMessage = documentField<string>(doc, "error_message");
  const chunksFailed = documentField<number>(doc, "chunks_failed");
  const progressPercent = documentField<number>(doc, "progress_percent");
  const processedUnits = documentField<number>(doc, "processed_units");
  const totalUnits = documentField<number>(doc, "total_units");
  const unitLabel = documentField<string>(doc, "unit_label");
  const isFailed = status === "error" || status === "failed";
  const isActive = status === "pending" || status === "processing";
  // LIVE-03: "partial" is a terminal completed-with-failures state — the
  // pipeline finished but some chunks failed to embed. Never render it as a
  // pending/"Waiting" state.
  const isPartial = status === "partial";
  const failureDescription =
    errorMessage ??
    (chunksFailed != null && chunksFailed > 0
      ? `${chunksFailed.toLocaleString()} chunks failed to embed`
      : null);
  const label = isPartial
    ? failureDescription ?? "Completed with failures"
    : phaseMessage ?? phase ?? (isFailed ? "Failed" : status === "indexed" ? "Complete" : "Waiting");
  const unitsText =
    processedUnits != null && totalUnits != null
      ? `${processedUnits.toLocaleString()} / ${totalUnits.toLocaleString()} ${
          unitLabel ?? ""
        }`.trim()
      : null;

  return {
    errorMessage,
    failureDescription,
    isActive,
    isFailed,
    isPartial,
    label,
    progressPercent,
    title:
      isFailed && errorMessage
        ? errorMessage
        : isPartial && failureDescription
          ? failureDescription
          : label,
    unitsText,
    shouldRender:
      isActive || isFailed || isPartial || progressPercent != null || Boolean(phaseMessage || phase),
  };
}

export function DocumentProgressCell({ doc }: { doc: Document }) {
  const progress = documentProgress(doc);

  if (!progress.shouldRender) {
    return <span className="text-muted-foreground">-</span>;
  }

  return (
    <div className="space-y-1" title={progress.title}>
      <div className="flex items-center justify-between gap-2 text-xs">
        <span
          className={cn(
            "truncate",
            progress.isFailed
              ? "text-destructive"
              : progress.isPartial
                ? "text-warning"
                : "text-muted-foreground"
          )}
        >
          {progress.label}
          {progress.unitsText ? ` - ${progress.unitsText}` : ""}
        </span>
        {progress.progressPercent != null && (
          <span className="tabular-nums text-muted-foreground">
            {Math.round(progress.progressPercent)}%
          </span>
        )}
      </div>
      {!progress.isFailed && !progress.isPartial && (
        <Progress
          value={progress.progressPercent ?? undefined}
          className="h-1.5"
          aria-label={`Processing progress for ${doc.filename}`}
        />
      )}
    </div>
  );
}
