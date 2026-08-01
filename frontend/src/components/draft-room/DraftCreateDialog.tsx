import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  DraftAssignmentForm,
  createDefaultDraftAssignmentFormValue,
  focusFirstInvalidDraftAssignmentField,
  type DraftAssignmentFormValue,
  validateDraftAssignmentForm,
} from "@/components/draft-room/DraftAssignmentForm";
import { CREATE_PROJECT_HEADING, DRAFT_ROOM_DISABLED_MESSAGE } from "@/components/draft-room/labels";
import {
  createDraft,
  draftRoomKeys,
  parseDraftRoomError,
  type DraftCreateRequest,
  type DraftRoomErrorInfo,
  type DraftSummary,
} from "@/lib/api/draftRoom";

export interface DraftCreateDialogProps {
  open: boolean;
  onOpenChange(open: boolean): void;
  /** Preselected vault, e.g. the list page's current filter. */
  defaultVaultId?: number | null;
  onCreated(draft: DraftSummary): void;
}

export function DraftCreateDialog(props: DraftCreateDialogProps): JSX.Element {
  const { open, onOpenChange, defaultVaultId, onCreated } = props;
  const queryClient = useQueryClient();
  const titleRef = useRef<HTMLHeadingElement>(null);
  const formContainerRef = useRef<HTMLDivElement>(null);

  const [value, setValue] = useState<DraftAssignmentFormValue>(() =>
    createDefaultDraftAssignmentFormValue(defaultVaultId)
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitError, setSubmitError] = useState<DraftRoomErrorInfo | null>(null);

  useEffect(() => {
    if (!open) return;
    setValue(createDefaultDraftAssignmentFormValue(defaultVaultId));
    setErrors({});
    setSubmitError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reset only on open transition, not on every defaultVaultId identity change
  }, [open]);

  const mutation = useMutation({
    mutationFn: (payload: DraftCreateRequest) => createDraft(payload),
    onSuccess: (draft) => {
      toast.success("Drafting project created.");
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.lists() });
      onCreated(draft);
      onOpenChange(false);
    },
    onError: (err) => {
      setSubmitError(parseDraftRoomError(err));
    },
  });

  function handleSubmit(e: React.FormEvent): void {
    e.preventDefault();
    setSubmitError(null);
    const nextErrors = validateDraftAssignmentForm(value, "create");
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) {
      focusFirstInvalidDraftAssignmentField(formContainerRef.current, nextErrors);
      return;
    }
    const payload: DraftCreateRequest = {
      vault_id: value.vault_id as number,
      title: value.title.trim(),
      mode: value.mode,
      tier: value.tier,
      brief: value.brief,
    };
    mutation.mutate(payload);
  }

  const isDisabled = mutation.isPending;
  const isUnavailable = submitError?.status === 503 && submitError.code === "draft_room_disabled";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto sm:max-w-xl"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          titleRef.current?.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle ref={titleRef} tabIndex={-1}>
            {CREATE_PROJECT_HEADING}
          </DialogTitle>
        </DialogHeader>

        {submitError && (
          <Alert variant={isUnavailable ? "warning" : "destructive"} role="alert">
            <AlertDescription>
              {isUnavailable
                ? DRAFT_ROOM_DISABLED_MESSAGE
                : submitError.detail}
            </AlertDescription>
          </Alert>
        )}

        <form onSubmit={handleSubmit} noValidate>
          <div ref={formContainerRef}>
            <DraftAssignmentForm
              value={value}
              onChange={setValue}
              errors={errors}
              variant="create"
              disabled={isDisabled}
              idPrefix="draft-create"
            />
          </div>
          <DialogFooter className="mt-6">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={isDisabled}>
              Cancel
            </Button>
            <Button type="submit" disabled={isDisabled}>
              Create project
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
