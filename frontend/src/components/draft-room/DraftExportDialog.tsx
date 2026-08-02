import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useDraftRoomCapabilities } from "@/hooks/useDraftRoomCapabilities";
import {
  FACT_CURRENT_STATUSES,
  exportDraftRevision,
  parseDraftRoomError,
  type DraftRevisionSummary,
} from "@/lib/api/draftRoom";
import {
  DRAFT_STATUS_LABELS,
  EXPORT_ACK_LABEL,
  EXPORT_CTA,
  EXPORT_READY_EXPLANATION,
  EXPORT_REVIEW_EXPLANATION,
  EXPORT_UNVERIFIED_EXPLANATION,
  FACT_STATUS_LABELS,
} from "@/components/draft-room/labels";

export interface DraftExportDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  draftId: number;
  revision: DraftRevisionSummary;
  isReadyRevision: boolean;
}

const FACT_CURRENT = new Set<string>(FACT_CURRENT_STATUSES);

const FORMAT_LABELS: Record<string, string> = {
  md: "Markdown (.md)",
};

/**
 * Triggers a browser download of `blob` under `filename` without touching a
 * single byte of it, then releases the object URL. Never prepend a warning
 * into the document — the warning lives in this dialog's copy only.
 */
function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

export function DraftExportDialog({
  open,
  onOpenChange,
  draftId,
  revision,
  isReadyRevision,
}: DraftExportDialogProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const { data: capabilities } = useDraftRoomCapabilities();
  const formats = capabilities?.export_formats ?? ["md"];
  const [format, setFormat] = useState(formats[0] ?? "md");
  const [ack, setAck] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ackRequired = !FACT_CURRENT.has(revision.fact_status);
  const filenameCase: "unverified" | "review" | "ready" = ackRequired
    ? "unverified"
    : isReadyRevision
      ? "ready"
      : "review";

  useEffect(() => {
    if (!open) {
      setAck(false);
      setExporting(false);
      setError(null);
    }
  }, [open]);

  const canExport = !exporting && (!ackRequired || ack);

  const handleExport = async () => {
    setExporting(true);
    setError(null);
    try {
      const result = await exportDraftRevision(draftId, revision.id, {
        format,
        acknowledge_not_fact_checked: ackRequired ? ack : false,
      });
      downloadBlob(result.blob, result.filename);
      toast.success(`Downloaded ${result.filename}`);
      onOpenChange(false);
    } catch (err) {
      setError(parseDraftRoomError(err).detail);
    } finally {
      setExporting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby="draft-export-description"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          headingRef.current?.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle ref={headingRef} tabIndex={-1}>
            {EXPORT_CTA}
          </DialogTitle>
          <DialogDescription id="draft-export-description">
            Revision {revision.revision_no}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-1 text-sm">
          <p>
            Fact status:{" "}
            <span className="font-medium">
              {FACT_STATUS_LABELS[revision.fact_status] ?? revision.fact_status}
            </span>
          </p>
          <p>
            Approval status:{" "}
            <span className="font-medium">
              {isReadyRevision ? DRAFT_STATUS_LABELS.ready : "Not ready"}
            </span>
          </p>
        </div>

        <div className="space-y-1">
          <Label htmlFor="draft-export-format">Format</Label>
          <Select value={format} onValueChange={setFormat} disabled={exporting}>
            <SelectTrigger id="draft-export-format">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {formats.map((f) => (
                <SelectItem key={f} value={f}>
                  {FORMAT_LABELS[f] ?? f}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <p className="text-sm text-muted-foreground">
          {filenameCase === "unverified"
            ? EXPORT_UNVERIFIED_EXPLANATION
            : filenameCase === "review"
              ? EXPORT_REVIEW_EXPLANATION
              : EXPORT_READY_EXPLANATION}
        </p>

        {ackRequired ? (
          <div className="flex items-start gap-2">
            <Checkbox
              id="export-ack"
              checked={ack}
              onCheckedChange={(checked) => setAck(checked === true)}
              disabled={exporting}
              aria-describedby="export-ack-label"
            />
            <Label htmlFor="export-ack" id="export-ack-label" className="mb-0 font-normal">
              {EXPORT_ACK_LABEL}
            </Label>
          </div>
        ) : null}

        {error ? (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={exporting}>
            Cancel
          </Button>
          <Button type="button" onClick={handleExport} disabled={!canExport}>
            {exporting ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                {EXPORT_CTA}
              </>
            ) : (
              EXPORT_CTA
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
