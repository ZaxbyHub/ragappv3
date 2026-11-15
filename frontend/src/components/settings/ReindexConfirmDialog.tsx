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
              className="h-5 w-5 text-warning"
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
          settings. After saving, search, chat, memories, and wiki pause
          with an embedding-mismatch 503 and new uploads are paused until
          you rebuild embeddings (all-vault reindex) from the Maintenance
          tab. The reindex must run to completion to restore readiness —
          restarting does not clear the 503.
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
            className="bg-warning hover:bg-warning text-warning-foreground"
          >
            {saving ? "Saving…" : "Save and acknowledge"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
