import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { cn } from "@/lib/utils";
import {
  markDraftRevisionReady,
  parseDraftRoomError,
  type DraftRevisionSummary,
  type DraftSummary,
} from "@/lib/api/draftRoom";
import { MARK_READY_CTA, READY_BLOCKER_LABELS, READY_MEANING } from "@/components/draft-room/labels";

export interface ReadyEligibility {
  ok: boolean;
  blockers: string[];
}

export interface DraftReadyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  draft: DraftSummary;
  revision: DraftRevisionSummary;
  eligibility: ReadyEligibility;
  requiresSourceOnlyAck: boolean;
  onReady: (summary: DraftSummary) => void;
}

/**
 * Ordered checklist of Ready eligibility conditions (SPEC 12.5). Every entry is a
 * possible `POST .../ready` 409 `code`, keyed against `READY_BLOCKER_LABELS`.
 * `invalid_state`, `not_current_revision`, `vault_access_revoked` and `conflict`
 * are route-level preconditions the shell already guards against opening this
 * dialog for, so they are surfaced as a fallback banner rather than a checklist
 * row (see `handleSubmit`'s catch branch).
 */
const CHECKLIST_CODES = [
  "active_job",
  "fact_not_current",
  "fact_candidate_mismatch",
  "unresolved_claim_blocker",
  "non_waivable_blocker",
  "unresolved_blocker",
  "invalid_waiver",
  "stale_waiver",
  "evidence_changed",
  "source_deleted",
  "source_only_acknowledgment_required",
] as const;

export function DraftReadyDialog({
  open,
  onOpenChange,
  draft,
  revision,
  eligibility,
  requiresSourceOnlyAck,
  onReady,
}: DraftReadyDialogProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [ack, setAck] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<{ code: string; detail: string } | null>(null);

  useEffect(() => {
    if (!open) {
      setAck(false);
      setSubmitting(false);
      setServerError(null);
    }
  }, [open]);

  const blockers = serverError
    ? Array.from(new Set([...eligibility.blockers, serverError.code]))
    : eligibility.blockers;
  const isBlocked = (code: string) => blockers.includes(code);
  const canSubmit =
    eligibility.ok && serverError === null && (!requiresSourceOnlyAck || ack) && !submitting;
  const shaPrefix = revision.content_sha256 ? revision.content_sha256.slice(0, 12) : "";
  const fallbackServerMessage =
    serverError && !(CHECKLIST_CODES as readonly string[]).includes(serverError.code)
      ? (READY_BLOCKER_LABELS[serverError.code] ?? serverError.detail)
      : null;

  const handleSubmit = async () => {
    setSubmitting(true);
    setServerError(null);
    try {
      const summary = await markDraftRevisionReady(draft.id, revision.id, {
        lock_version: draft.lock_version,
        acknowledge_source_only: ack,
      });
      toast.success(`${MARK_READY_CTA} — revision ${revision.revision_no}`);
      onReady(summary);
      onOpenChange(false);
    } catch (err) {
      const info = parseDraftRoomError(err);
      setServerError({ code: info.code, detail: info.detail });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby="draft-ready-meaning"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          headingRef.current?.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle ref={headingRef} tabIndex={-1}>
            {MARK_READY_CTA}
          </DialogTitle>
          <DialogDescription id="draft-ready-meaning">{READY_MEANING}</DialogDescription>
        </DialogHeader>

        <p className="text-sm text-muted-foreground">
          Revision {revision.revision_no} ·{" "}
          <span className="font-mono" data-testid="ready-revision-sha">
            {shaPrefix}
          </span>
        </p>

        <ul className="space-y-2" aria-label="Ready eligibility checklist">
          {CHECKLIST_CODES.map((code) => {
            const failed = isBlocked(code);
            return (
              <li key={code} className="flex items-start gap-2 text-sm">
                {failed ? (
                  <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
                ) : (
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-hidden="true" />
                )}
                <span>
                  <span className={cn("font-medium", failed ? "text-destructive" : "text-success")}>
                    {failed ? "Blocked:" : "Clear:"}
                  </span>{" "}
                  {READY_BLOCKER_LABELS[code]}
                </span>
              </li>
            );
          })}
        </ul>

        {fallbackServerMessage ? (
          <Alert variant="destructive">
            <AlertDescription>{fallbackServerMessage}</AlertDescription>
          </Alert>
        ) : null}

        {requiresSourceOnlyAck ? (
          <div className="flex items-start gap-2">
            <Checkbox
              id="ready-source-only-ack"
              checked={ack}
              onCheckedChange={(checked) => setAck(checked === true)}
              disabled={submitting}
              aria-describedby="ready-source-only-ack-label"
            />
            <Label htmlFor="ready-source-only-ack" id="ready-source-only-ack-label" className="mb-0 font-normal">
              {READY_BLOCKER_LABELS.source_only_acknowledgment_required} I want to mark this Ready anyway.
            </Label>
          </div>
        ) : null}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button type="button" onClick={handleSubmit} disabled={!canSubmit}>
            {submitting ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                {MARK_READY_CTA}
              </>
            ) : (
              MARK_READY_CTA
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
