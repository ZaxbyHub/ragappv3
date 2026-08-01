import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Link } from "react-router-dom";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { getVault, listFolders, listTags } from "@/lib/api";
import {
  draftRoomKeys,
  parseDraftRoomError,
  promoteDraftSource,
  type DraftInput,
  type DraftPromoteSourceType,
  type DraftRevisionSummary,
  type DraftSummary,
  type PromoteResponse,
} from "@/lib/api/draftRoom";
import { FACT_STATUS_LABELS, PROMOTE_CONSEQUENCE, PROMOTE_TO_VAULT_CTA } from "@/components/draft-room/labels";

export interface DraftPromoteDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  draft: DraftSummary;
  inputs: DraftInput[];
  revisions: DraftRevisionSummary[];
  currentRevisionId: number | null;
  canWrite: boolean;
  onPromoted: (result: PromoteResponse) => void;
}

const TITLE_MAX_LENGTH = 300;

const PROMOTE_ERROR_MESSAGES: Record<string, string> = {
  duplicate_document: "This exact content has already been promoted to this vault.",
  vault_access_revoked: "You no longer have write access to this project's vault.",
};

export function DraftPromoteDialog({
  open,
  onOpenChange,
  draft,
  inputs,
  revisions,
  currentRevisionId,
  canWrite,
  onPromoted,
}: DraftPromoteDialogProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const titleInputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();

  const [sourceType, setSourceType] = useState<DraftPromoteSourceType>(
    inputs.length > 0 ? "input" : "revision"
  );
  const [sourceId, setSourceId] = useState<number | null>(null);
  const [title, setTitle] = useState(draft.title);
  const [titleError, setTitleError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [folderId, setFolderId] = useState<number | null>(null);
  const [tagIds, setTagIds] = useState<number[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PromoteResponse | null>(null);

  const { data: vault } = useQuery({
    queryKey: ["vault", draft.vault_id],
    queryFn: () => getVault(draft.vault_id),
    enabled: open,
  });
  const { data: folders } = useQuery({
    queryKey: ["folders", draft.vault_id],
    queryFn: () => listFolders(draft.vault_id),
    enabled: open,
  });
  const { data: tags } = useQuery({
    queryKey: ["tags", draft.vault_id],
    queryFn: () => listTags(draft.vault_id),
    enabled: open,
  });

  useEffect(() => {
    if (!open) return;
    headingRef.current?.focus();
    setSourceType(inputs.length > 0 ? "input" : "revision");
    setTitle(draft.title);
    setTitleError(null);
    setConfirmed(false);
    setFolderId(null);
    setTagIds([]);
    setSubmitting(false);
    setError(null);
    setResult(null);
    // Re-initialize only when the dialog opens for this draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, draft.id]);

  useEffect(() => {
    if (!open) return;
    if (sourceType === "revision") {
      const defaultId =
        currentRevisionId != null && revisions.some((r) => r.id === currentRevisionId)
          ? currentRevisionId
          : (revisions[0]?.id ?? null);
      setSourceId(defaultId);
    } else {
      setSourceId(inputs[0]?.id ?? null);
    }
    // Only re-derive the default when the source type (or the dialog) changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, sourceType]);

  const canWriteExplanation = !canWrite
    ? "You have read-only access to this project's vault. Promotion requires vault write permission."
    : null;

  const trimmedTitle = title.trim();
  const canSubmit =
    canWrite &&
    !submitting &&
    result === null &&
    sourceId !== null &&
    trimmedTitle.length >= 1 &&
    trimmedTitle.length <= TITLE_MAX_LENGTH &&
    confirmed;

  const validateTitle = (): boolean => {
    if (trimmedTitle.length < 1) {
      setTitleError("Title is required.");
      titleInputRef.current?.focus();
      return false;
    }
    if (trimmedTitle.length > TITLE_MAX_LENGTH) {
      setTitleError(`Title must be ${TITLE_MAX_LENGTH} characters or fewer.`);
      titleInputRef.current?.focus();
      return false;
    }
    setTitleError(null);
    return true;
  };

  const handleSubmit = async () => {
    if (!validateTitle() || sourceId === null) return;
    setSubmitting(true);
    setError(null);
    try {
      const response = await promoteDraftSource(draft.id, {
        source_type: sourceType,
        source_id: sourceId,
        title: trimmedTitle,
        folder_id: folderId,
        tag_ids: tagIds,
      });
      setResult(response);
      toast.success(`Promoted "${response.filename}" to the vault`);
      // DocumentsPage does not use React Query, so there is no documents cache to
      // invalidate here. The promoted file is surfaced by link instead, and its
      // ingestion status is read from the documents API on that page's own load.
      queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(draft.id) });
      onPromoted(response);
    } catch (err) {
      const info = parseDraftRoomError(err);
      setError(PROMOTE_ERROR_MESSAGES[info.code] ?? info.detail);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent aria-describedby="draft-promote-description">
        <DialogHeader>
          <DialogTitle ref={headingRef} tabIndex={-1}>
            {PROMOTE_TO_VAULT_CTA}
          </DialogTitle>
          <DialogDescription id="draft-promote-description">{PROMOTE_CONSEQUENCE}</DialogDescription>
        </DialogHeader>

        {result ? (
          <div className="space-y-4">
            <Alert variant="success">
              <AlertDescription>
                Promoted as{" "}
                <Link to={`/documents/${result.file_id}`} className="font-medium underline">
                  {result.filename}
                </Link>{" "}
                (document #{result.file_id}). The new document is queued for indexing — it is not
                indexed yet.
              </AlertDescription>
            </Alert>
            <DialogFooter>
              <Button type="button" onClick={() => onOpenChange(false)}>
                Close
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <>
            {canWriteExplanation ? (
              <Alert variant="warning">
                <AlertDescription>{canWriteExplanation}</AlertDescription>
              </Alert>
            ) : null}

            <fieldset disabled={!canWrite || submitting} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="promote-source-type-input">Source</Label>
                <RadioGroup
                  orientation="vertical"
                  value={sourceType}
                  onValueChange={(value) => setSourceType(value as DraftPromoteSourceType)}
                  name="promote-source-type"
                >
                  <div className="flex items-center gap-2">
                    <RadioGroupItem
                      id="promote-source-type-input"
                      value="input"
                      disabled={inputs.length === 0}
                    />
                    <Label htmlFor="promote-source-type-input" className="mb-0 font-normal">
                      A source file
                    </Label>
                  </div>
                  <div className="flex items-center gap-2">
                    <RadioGroupItem
                      id="promote-source-type-revision"
                      value="revision"
                      disabled={revisions.length === 0}
                    />
                    <Label htmlFor="promote-source-type-revision" className="mb-0 font-normal">
                      A draft revision
                    </Label>
                  </div>
                </RadioGroup>
              </div>

              <div className="space-y-2">
                <Label htmlFor="promote-source-id">
                  {sourceType === "input" ? "Source file" : "Revision"}
                </Label>
                <Select
                  value={sourceId != null ? String(sourceId) : undefined}
                  onValueChange={(value) => setSourceId(Number(value))}
                >
                  <SelectTrigger id="promote-source-id">
                    <SelectValue placeholder="Select a source" />
                  </SelectTrigger>
                  <SelectContent>
                    {sourceType === "input"
                      ? inputs.map((input) => (
                          <SelectItem key={input.id} value={String(input.id)}>
                            {input.original_name}
                          </SelectItem>
                        ))
                      : revisions.map((revision) => (
                          <SelectItem key={revision.id} value={String(revision.id)}>
                            Revision {revision.revision_no}
                            {revision.id === currentRevisionId ? " (current)" : ""} ·{" "}
                            {FACT_STATUS_LABELS[revision.fact_status] ?? revision.fact_status}
                          </SelectItem>
                        ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label htmlFor="promote-destination-vault">Destination vault</Label>
                <Input
                  id="promote-destination-vault"
                  value={vault?.name ?? `Vault #${draft.vault_id}`}
                  readOnly
                  disabled
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="promote-title">Document title</Label>
                <Input
                  id="promote-title"
                  ref={titleInputRef}
                  value={title}
                  onChange={(e) => {
                    setTitle(e.target.value);
                    if (titleError) setTitleError(null);
                  }}
                  maxLength={TITLE_MAX_LENGTH}
                  aria-invalid={titleError != null}
                  aria-describedby={titleError ? "promote-title-error" : undefined}
                />
                {titleError ? (
                  <p id="promote-title-error" className="text-sm text-destructive">
                    {titleError}
                  </p>
                ) : null}
              </div>

              <div className="space-y-2">
                <Label htmlFor="promote-folder">Folder (optional)</Label>
                <Select
                  value={folderId != null ? String(folderId) : "none"}
                  onValueChange={(value) => setFolderId(value === "none" ? null : Number(value))}
                >
                  <SelectTrigger id="promote-folder">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">No folder</SelectItem>
                    {(folders ?? []).map((folder) => (
                      <SelectItem key={folder.id} value={String(folder.id)}>
                        {folder.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label id="promote-tags-label">Tags (optional)</Label>
                <div className="flex flex-wrap gap-x-4 gap-y-2" role="group" aria-labelledby="promote-tags-label">
                  {(tags ?? []).length === 0 ? (
                    <p className="text-sm text-muted-foreground">No tags in this vault yet.</p>
                  ) : (
                    (tags ?? []).map((tag) => (
                      <div key={tag.id} className="flex items-center gap-1.5">
                        <Checkbox
                          id={`promote-tag-${tag.id}`}
                          checked={tagIds.includes(tag.id)}
                          onCheckedChange={(checked) =>
                            setTagIds((prev) =>
                              checked === true ? [...prev, tag.id] : prev.filter((id) => id !== tag.id)
                            )
                          }
                        />
                        <Label htmlFor={`promote-tag-${tag.id}`} className="mb-0 font-normal">
                          {tag.name}
                        </Label>
                      </div>
                    ))
                  )}
                </div>
              </div>

              <div className="flex items-start gap-2">
                <Checkbox
                  id="promote-confirm"
                  checked={confirmed}
                  onCheckedChange={(checked) => setConfirmed(checked === true)}
                  aria-describedby="draft-promote-description"
                />
                <Label htmlFor="promote-confirm" className="mb-0 font-normal">
                  I understand and want to promote this content to the vault.
                </Label>
              </div>
            </fieldset>

            {error ? (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            ) : null}

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
                Cancel
              </Button>
              <Button type="button" onClick={handleSubmit} disabled={!canSubmit}>
                {submitting ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                    {PROMOTE_TO_VAULT_CTA}
                  </>
                ) : (
                  PROMOTE_TO_VAULT_CTA
                )}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
