import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2, Clock, FileText, Loader2, Pencil, Trash2, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatDate, formatFileSize } from "@/lib/formatters";
import { INPUT_AUTHORITY_LABELS, INPUT_ROLE_LABELS } from "./labels";
import {
  DRAFT_INPUT_AUTHORITIES,
  DRAFT_INPUT_ROLES,
  deleteDraftInput,
  draftRoomKeys,
  getDraftInputContent,
  parseDraftRoomError,
  updateDraftInput,
  type DraftInput,
  type DraftInputAuthority,
  type DraftInputParseStatus,
  type DraftInputRole,
  type InputUpdateRequest,
} from "@/lib/api/draftRoom";

export interface DraftSourceListProps {
  draftId: number;
  inputs: DraftInput[];
  /** Editing is blocked while a compile job is active. */
  locked: boolean;
  lockedReason?: string;
  canEdit: boolean;
}

const PARSE_STATUS_TEXT: Record<DraftInputParseStatus, string> = {
  pending: "Queued to parse",
  parsing: "Parsing",
  ready: "Parsed",
  failed: "Parse failed",
  cancelled: "Parsing cancelled",
};

function ParseStatusIndicator({ status }: { status: DraftInputParseStatus }) {
  const text = PARSE_STATUS_TEXT[status];
  switch (status) {
    case "ready":
      return (
        <span className="flex items-center gap-1 text-xs text-success">
          <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
          {text}
        </span>
      );
    case "failed":
    case "cancelled":
      return (
        <span className="flex items-center gap-1 text-xs text-destructive">
          <XCircle className="h-3.5 w-3.5" aria-hidden="true" />
          {text}
        </span>
      );
    case "parsing":
      return (
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          {text}
        </span>
      );
    default:
      return (
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <Clock className="h-3.5 w-3.5" aria-hidden="true" />
          {text}
        </span>
      );
  }
}

/**
 * Read-only disclosure for what the pipeline actually extracted from an
 * upload. Fetches lazily on open (the payload can be very large) rather than
 * on list render, and never on inputs whose parse hasn't produced text yet.
 */
function DraftParsedTextViewer({ draftId, input }: { draftId: number; input: DraftInput }) {
  const [open, setOpen] = useState(false);
  const panelId = `draft-source-${input.id}-parsed-text`;

  const contentQuery = useQuery({
    queryKey: draftRoomKeys.inputContent(draftId, input.id),
    queryFn: () => getDraftInputContent(draftId, input.id),
    enabled: open,
  });

  return (
    <div className="mt-2">
      <Button
        type="button"
        size="sm"
        variant="ghost"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        aria-controls={panelId}
      >
        {open ? "Hide parsed text" : "View parsed text"}
      </Button>
      {open && (
        <div id={panelId} className="mt-2 min-w-0">
          {contentQuery.isLoading && (
            <p className="text-sm text-muted-foreground">Loading parsed text…</p>
          )}
          {contentQuery.isError && (
            <p className="text-sm text-destructive">{parseDraftRoomError(contentQuery.error).detail}</p>
          )}
          {contentQuery.data && (
            <pre className="max-h-64 min-w-0 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/30 p-2 text-xs">
              {contentQuery.data.parsed_text ?? "No parsed text available."}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

interface DraftSourceRowProps {
  draftId: number;
  input: DraftInput;
  locked: boolean;
  lockedReason?: string;
  canEdit: boolean;
}

function DraftSourceRow({ draftId, input, locked, lockedReason, canEdit }: DraftSourceRowProps) {
  const queryClient = useQueryClient();
  const [isEditing, setIsEditing] = useState(false);
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
  const [editRole, setEditRole] = useState<DraftInputRole>(input.role);
  const [editAuthority, setEditAuthority] = useState<DraftInputAuthority>(input.authority);
  const [editAsOfDate, setEditAsOfDate] = useState(input.as_of_date ?? "");
  const deleteHeadingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (!confirmDeleteOpen) return;
    const frame = requestAnimationFrame(() => deleteHeadingRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [confirmDeleteOpen]);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draftId) });
    queryClient.invalidateQueries({ queryKey: draftRoomKeys.inputs(draftId) });
  };

  const updateMutation = useMutation({
    mutationFn: (data: InputUpdateRequest) => updateDraftInput(draftId, input.id, data),
    onSuccess: () => {
      toast.success("Source updated");
      setIsEditing(false);
      invalidate();
    },
    onError: (err) => {
      toast.error(parseDraftRoomError(err).detail);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteDraftInput(draftId, input.id),
    onSuccess: () => {
      toast.success("Source removed");
      setConfirmDeleteOpen(false);
      invalidate();
    },
    onError: (err) => {
      toast.error(parseDraftRoomError(err).detail);
    },
  });

  const startEdit = () => {
    setEditRole(input.role);
    setEditAuthority(input.authority);
    setEditAsOfDate(input.as_of_date ?? "");
    setIsEditing(true);
  };

  const saveEdit = () => {
    const payload: InputUpdateRequest = { role: editRole, authority: editAuthority };
    if (editAsOfDate) {
      payload.as_of_date = editAsOfDate;
    } else if (input.as_of_date) {
      payload.clear_as_of_date = true;
    }
    updateMutation.mutate(payload);
  };

  const editingDisabled = locked || !canEdit;
  const idBase = `draft-source-${input.id}`;

  return (
    <li className="min-w-0 rounded-md border border-border p-3">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <FileText className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span className="min-w-0 truncate text-sm font-medium" title={input.original_name}>
              {input.original_name}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span>{input.extension || "unknown type"}</span>
            <span>{formatFileSize(input.size_bytes)}</span>
            <span>{INPUT_ROLE_LABELS[input.role]}</span>
            <span>{INPUT_AUTHORITY_LABELS[input.authority]}</span>
            <span>As of {input.as_of_date ? formatDate(input.as_of_date) : "unknown"}</span>
            {input.parsed_char_count != null && (
              <span>{input.parsed_char_count.toLocaleString()} characters parsed</span>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <ParseStatusIndicator status={input.parse_status} />
          {canEdit && !isEditing && (
            <>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={startEdit}
                disabled={editingDisabled}
                aria-label={`Edit ${input.original_name}`}
              >
                <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => setConfirmDeleteOpen(true)}
                disabled={editingDisabled}
                aria-label={`Remove ${input.original_name}`}
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
              </Button>
            </>
          )}
        </div>
      </div>

      {canEdit && locked && (
        <p className="mt-2 text-xs text-muted-foreground" role="status">
          Editing is unavailable while a newsroom run is active{lockedReason ? `: ${lockedReason}` : "."}
        </p>
      )}

      {(input.parse_status === "failed" || input.parse_status === "cancelled") && (
        <div className="mt-2 space-y-1">
          {input.parse_error && <p className="text-sm text-destructive">{input.parse_error}</p>}
          <p className="text-xs text-muted-foreground">
            To retry, remove this source and upload the file again.
          </p>
        </div>
      )}

      {input.parse_status === "ready" && <DraftParsedTextViewer draftId={draftId} input={input} />}

      {canEdit && isEditing && (
        <div className="mt-3 space-y-3 border-t border-border pt-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor={`${idBase}-role`}>Role</Label>
              <Select value={editRole} onValueChange={(value) => setEditRole(value as DraftInputRole)}>
                <SelectTrigger id={`${idBase}-role`} className="w-40" aria-label="Role">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DRAFT_INPUT_ROLES.map((value) => (
                    <SelectItem key={value} value={value}>
                      {INPUT_ROLE_LABELS[value]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${idBase}-authority`}>Authority</Label>
              <Select
                value={editAuthority}
                onValueChange={(value) => setEditAuthority(value as DraftInputAuthority)}
              >
                <SelectTrigger id={`${idBase}-authority`} className="w-40" aria-label="Authority">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DRAFT_INPUT_AUTHORITIES.map((value) => (
                    <SelectItem key={value} value={value}>
                      {INPUT_AUTHORITY_LABELS[value]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${idBase}-as-of`}>As-of date</Label>
              <Input
                id={`${idBase}-as-of`}
                type="date"
                value={editAsOfDate}
                onChange={(event) => setEditAsOfDate(event.target.value)}
                className="w-40"
              />
            </div>
          </div>
          <div className="flex gap-2">
            <Button type="button" size="sm" onClick={saveEdit} disabled={updateMutation.isPending}>
              Save
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setIsEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      <Dialog open={confirmDeleteOpen} onOpenChange={setConfirmDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle ref={deleteHeadingRef} tabIndex={-1}>
              Remove {input.original_name}?
            </DialogTitle>
            <DialogDescription>
              This permanently deletes the source file from this project. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setConfirmDeleteOpen(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => deleteMutation.mutate()}
              disabled={deleteMutation.isPending}
            >
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </li>
  );
}

export function DraftSourceList({ draftId, inputs, locked, lockedReason, canEdit }: DraftSourceListProps) {
  if (inputs.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
        No source files yet. A compile needs at least one parsed, ready source — add a file above to
        get started.
      </div>
    );
  }

  return (
    <ul className="min-w-0 space-y-3">
      {inputs.map((input) => (
        <DraftSourceRow
          key={input.id}
          draftId={draftId}
          input={input}
          locked={locked}
          lockedReason={lockedReason}
          canEdit={canEdit}
        />
      ))}
    </ul>
  );
}
