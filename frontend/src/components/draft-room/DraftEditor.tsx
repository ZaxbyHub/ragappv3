import { useEffect, useRef, useState } from "react";

import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { DraftRevisionDetail } from "@/lib/api/draftRoom";
import { cn } from "@/lib/utils";

import { SAVE_REVISION_CONSEQUENCE } from "./labels";

export interface DraftEditorProps {
  draftId: number;
  revision: DraftRevisionDetail | null;
  value: string;
  onChange(next: string): void;
  disabled?: boolean;
  disabledReason?: string;
}

/**
 * Controlled Markdown editor for a Draft Room revision. Deliberately a plain
 * `<Textarea>` — no rich-text or code editor framework, no autosave
 * (SPEC 16.5). The save mutation itself lives in the workspace shell; this
 * component only reports text changes and surfaces the dirty state.
 */
export function DraftEditor({
  draftId,
  revision,
  value,
  onChange,
  disabled,
  disabledReason,
}: DraftEditorProps) {
  const textareaId = `draft-editor-${draftId}`;
  const disabledReasonId = `${textareaId}-disabled-reason`;

  const isDirty = revision != null && value !== revision.content_md;

  // Announce the dirty state politely, but only on the transition into or
  // out of "dirty" — never on every keystroke.
  const [dirtyAnnouncement, setDirtyAnnouncement] = useState("");
  const previousDirtyRef = useRef(isDirty);
  useEffect(() => {
    if (previousDirtyRef.current !== isDirty) {
      previousDirtyRef.current = isDirty;
      setDirtyAnnouncement(isDirty ? "Unsaved changes." : "No unsaved changes.");
    }
  }, [isDirty]);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={textareaId}>Draft content</Label>
        {isDirty && (
          <span className="text-xs font-medium text-warning">Unsaved changes</span>
        )}
      </div>
      <div aria-live="polite" className="sr-only">
        {dirtyAnnouncement}
      </div>
      <Textarea
        id={textareaId}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        readOnly={disabled}
        aria-readonly={disabled}
        aria-describedby={disabled && disabledReason ? disabledReasonId : undefined}
        className={cn(
          "min-h-[420px] max-w-[80ch] font-mono text-sm leading-relaxed",
          disabled && "bg-muted/40"
        )}
        placeholder="Write or paste the manuscript in Markdown…"
      />
      {disabled && disabledReason && (
        <p id={disabledReasonId} className="text-sm text-muted-foreground">
          {disabledReason}
        </p>
      )}
      {isDirty && (
        <p className="max-w-[80ch] text-xs text-muted-foreground">{SAVE_REVISION_CONSEQUENCE}</p>
      )}
    </div>
  );
}
