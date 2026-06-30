// frontend/src/components/chat/CitationConfidence.tsx
import { cn } from "@/lib/utils";

interface CitationConfidenceProps {
  /**
   * Confidence score between 0 and 1.
   * - >= 0.7 → green (high confidence)
   * - >= 0.4 → amber (medium confidence)
   * - < 0.4 → red (low confidence)
   * - undefined/null → no indicator rendered
   */
  score?: number;
  /** Accessible label for the confidence indicator (defaults to score percentage) */
  label?: string;
  className?: string;
}

const CONFIDENCE_THRESHOLDS = {
  high: 0.7,
  medium: 0.4,
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
