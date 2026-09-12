/**
 * Quality-report dialog (issue #237, PRODUCT-ENH-12).
 *
 * Lets a user report a quality problem with a category — incorrect_answer,
 * missing_source, stale_source, bad_extraction — and an optional note,
 * bound to the exact message. Operators convert these reports into
 * replayable evaluation cases with expected outcome and provenance.
 */
import { useState } from "react";
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
import { toast } from "sonner";
import {
  QUALITY_REPORT_CATEGORIES,
  submitQualityReport,
  type QualityReportCategory,
} from "@/lib/api/qualityReports";

const CATEGORY_LABELS: Record<QualityReportCategory, string> = {
  incorrect_answer: "Incorrect answer",
  missing_source: "Missing source",
  stale_source: "Stale source",
  bad_extraction: "Bad extraction",
};

interface QualityReportDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: number;
  messageId: number;
}

export function QualityReportDialog({
  open,
  onOpenChange,
  sessionId,
  messageId,
}: QualityReportDialogProps) {
  const [category, setCategory] = useState<QualityReportCategory | null>(null);
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleOpenChange = (next: boolean) => {
    if (!next) {
      setCategory(null);
      setNote("");
      setSubmitting(false);
    }
    onOpenChange(next);
  };

  const handleSubmit = async () => {
    if (!category) return;
    setSubmitting(true);
    try {
      await submitQualityReport(sessionId, messageId, category, note || undefined);
      toast.success("Quality report submitted");
      handleOpenChange(false);
    } catch {
      toast.error("Couldn't submit quality report");
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        aria-labelledby="quality-report-title"
        aria-describedby="quality-report-desc"
      >
        <DialogHeader>
          <DialogTitle id="quality-report-title">Report a quality problem</DialogTitle>
          <DialogDescription id="quality-report-desc">
            What went wrong with this answer? Operators use these reports to
            build replayable evaluation cases.
          </DialogDescription>
        </DialogHeader>

        <fieldset className="grid gap-2" data-testid="quality-report-categories">
          <legend className="sr-only">Problem category</legend>
          {QUALITY_REPORT_CATEGORIES.map((value) => (
            <label
              key={value}
              className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm cursor-pointer has-[input:checked]:border-primary has-[input:checked]:bg-accent"
            >
              <input
                type="radio"
                name="quality-report-category"
                value={value}
                checked={category === value}
                onChange={() => setCategory(value)}
                data-testid={`quality-category-${value}`}
              />
              {CATEGORY_LABELS[value]}
            </label>
          ))}
        </fieldset>

        <div className="grid gap-2">
          <Label htmlFor="quality-report-note">Note (optional)</Label>
          <Input
            id="quality-report-note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="What should have happened?"
            maxLength={2000}
          />
        </div>

        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={!category || submitting}
            data-testid="quality-report-submit"
          >
            {submitting ? "Submitting…" : "Submit report"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
