/**
 * Sticky save/discard footer for the Settings page.
 *
 * Visible across tab switches so the user never loses sight of unsaved
 * changes. Save is disabled when invalid; Discard is disabled when
 * clean. Surfaces the count of dirty fields and a per-tab summary so the
 * user knows what's pending without scrolling.
 *
 * When idle (clean, not saving) the region is removed from the
 * accessibility tree via aria-hidden rather than merely faded out, and
 * the status text renders nothing — an idle page must not expose an
 * "Unsaved changes" region or an unsolicited "Saving…" to assistive tech.
 * The saving indicator (save in flight) stays exposed.
 */
import { Button } from "@/components/ui/button";
import { Loader2, Undo2 } from "lucide-react";
import { cn } from "@/lib/utils";

export interface SaveDiscardFooterProps {
  dirtyCount: number;
  invalid: boolean;
  saving: boolean;
  onSave: () => void;
  onDiscard: () => void;
}

export function SaveDiscardFooter({
  dirtyCount,
  invalid,
  saving,
  onSave,
  onDiscard,
}: SaveDiscardFooterProps) {
  const visible = dirtyCount > 0 || saving;
  return (
    <div
      className={cn(
        "sticky bottom-0 inset-x-0 z-10 -mx-4 mt-6 border-t bg-card/95 backdrop-blur-sm",
        "transition-all",
        visible ? "translate-y-0 opacity-100" : "pointer-events-none translate-y-2 opacity-0",
      )}
      role="region"
      aria-label="Unsaved changes"
      aria-hidden={!visible}
    >
      <div className="flex items-center justify-between gap-4 px-4 py-3">
        <div className="text-sm">
          {invalid ? (
            <span className="text-destructive">
              Fix highlighted errors before saving
            </span>
          ) : dirtyCount > 0 ? (
            <span>
              {dirtyCount} unsaved {dirtyCount === 1 ? "change" : "changes"}
            </span>
          ) : saving ? (
            <span className="text-muted-foreground">Saving…</span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={onDiscard}
            disabled={dirtyCount === 0 || saving}
            aria-label="Discard unsaved changes"
          >
            <Undo2 className="w-4 h-4 mr-1" />
            Discard
          </Button>
          <Button
            size="sm"
            onClick={onSave}
            disabled={invalid || dirtyCount === 0 || saving}
          >
            {saving && <Loader2 className="w-4 h-4 mr-1 animate-spin" />}
            Save Changes
          </Button>
        </div>
      </div>
    </div>
  );
}
