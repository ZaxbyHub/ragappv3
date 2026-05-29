import { AlertTriangle } from "lucide-react";

/**
 * Inline warning rendered beneath fields whose change invalidates existing
 * document embeddings. Always visible (not gated on dirty state) so the user
 * knows the consequence *before* they edit.
 *
 * Pair this with the ReindexConfirmDialog rendered by SettingsPage when any
 * dirty field belongs to REINDEX_REQUIRED_FIELDS at save time.
 */
export function ReindexFieldWarning({
  message = "Changing this requires re-indexing existing documents. Stored embeddings will be stale until re-indexed.",
}: {
  message?: string;
}) {
  return (
    <div
      role="note"
      className="flex items-start gap-1.5 rounded-sm border border-amber-500/40 bg-amber-500/5 px-2.5 py-1.5 text-xs text-amber-800 dark:text-amber-300"
    >
      <AlertTriangle
        className="h-3.5 w-3.5 flex-shrink-0 mt-0.5"
        aria-hidden="true"
      />
      <span>{message}</span>
    </div>
  );
}
