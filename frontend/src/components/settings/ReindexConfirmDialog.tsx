import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/**
 * Pre-save confirmation dialog that intercepts the Save button when any
 * dirty field belongs to REINDEX_REQUIRED_FIELDS. Listing the specific
 * dirty fields tells the user exactly what changed and forces an explicit
 * acknowledgement that existing embeddings will be stale until re-indexed.
 */
export function ReindexConfirmDialog({
  open,
  onOpenChange,
  dirtyReindexFields,
  onConfirm,
  saving,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  dirtyReindexFields: string[];
  onConfirm: () => void;
  saving: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle
              className="h-5 w-5 text-amber-600 dark:text-amber-400"
              aria-hidden="true"
            />
            Re-index required
          </DialogTitle>
          <DialogDescription>
            You are about to save changes to{" "}
            {dirtyReindexFields.length === 1 ? "a field" : "fields"} that
            affect how documents are embedded:
          </DialogDescription>
        </DialogHeader>

        <ul className="my-2 list-disc space-y-1 pl-6 text-sm text-foreground">
          {dirtyReindexFields.map((field) => (
            <li key={field}>
              <code className="rounded-sm bg-muted px-1 py-0.5 text-xs">
                {field}
              </code>
            </li>
          ))}
        </ul>

        <p className="text-sm text-muted-foreground">
          Existing document embeddings were generated with the previous
          settings. They will remain queryable but may produce stale or lower-
          quality results until you re-index. New documents will use the new
          settings immediately.
        </p>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={saving}
          >
            Cancel
          </Button>
          <Button
            onClick={onConfirm}
            disabled={saving}
            className="bg-amber-600 hover:bg-amber-700 text-white"
          >
            {saving ? "Saving…" : "Save and acknowledge"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
