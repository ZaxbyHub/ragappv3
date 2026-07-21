// frontend/src/components/chat/CitationConfidence.tsx
import { cn } from "@/lib/utils";

interface CitationConfidenceProps {
  /**
   * Confidence score between 0 and 1.
   * - >= 0.8 → green (high confidence)
   * - >= 0.5 → amber (medium confidence)
   * - < 0.5 → red (low confidence)
   * - undefined/null → no indicator rendered
   */
  score?: number;
  /** Accessible label for the confidence indicator (defaults to score percentage) */
  label?: string;
  className?: string;
}

// Calibrated for the backend's containment metric (|claim ∩ source| / |claim|):
// a verbatim-supported citation scores ~1.0 and a grounded paraphrase ~0.5-0.8.
// The previous 0.7/0.4 bands were tuned for the old Jaccard metric, whose
// scores rarely exceeded ~0.5 even for strong matches.
const CONFIDENCE_THRESHOLDS = {
  high: 0.8,
  medium: 0.5,
} as const;

const CONFIDENCE_COLORS = {
  high: "bg-emerald-500",
  medium: "bg-amber-500",
  low: "bg-red-500",
} as const;

function getConfidenceLevel(score: number): keyof typeof CONFIDENCE_COLORS {
  if (score >= CONFIDENCE_THRESHOLDS.high) return "high";
  if (score >= CONFIDENCE_THRESHOLDS.medium) return "medium";
  return "low";
}

/**
 * Renders a small colored dot indicating citation confidence.
 * Gracefully renders nothing when score is absent.
 */
export function CitationConfidence({ score, label, className }: CitationConfidenceProps) {
  if (score === undefined || score === null) {
    return null;
  }

  const level = getConfidenceLevel(score);
  const colorClass = CONFIDENCE_COLORS[level];
  const accessibleLabel = label ?? `${Math.round(score * 100)}% confidence`;

  return (
    <span
      className={cn(
        "inline-block h-2 w-2 flex-shrink-0 rounded-full",
        colorClass,
        className
      )}
      title={accessibleLabel}
      aria-label={accessibleLabel}
      role="img"
    />
  );
}
