// frontend/src/components/chat/CitationConfidence.tsx
import { useId } from "react";
import { cn } from "@/lib/utils";

interface CitationConfidenceProps {
  /**
   * Textual-overlap score between 0 and 1.
   * - >= 0.7 → green (high overlap)
   * - >= 0.4 → amber (medium overlap)
   * - < 0.4 → red (low overlap)
   * - undefined/null → no indicator rendered
   */
  score?: number;
  /** Accessible label for the overlap indicator (defaults to the overlap percentage) */
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

const OVERLAP_DESCRIPTION =
  "Word overlap between the claim and the cited passage — a lexical measure, not a probability of correctness; distinct from retrieval relevance.";

function getConfidenceLevel(score: number): keyof typeof CONFIDENCE_COLORS {
  if (score >= CONFIDENCE_THRESHOLDS.high) return "high";
  if (score >= CONFIDENCE_THRESHOLDS.medium) return "medium";
  return "low";
}

/**
 * Renders a small colored dot indicating how much of the cited passage's
 * wording the claim overlaps. Gracefully renders nothing when score is absent.
 */
export function CitationConfidence({ score, label, className }: CitationConfidenceProps) {
  const descriptionId = useId();
  if (score === undefined || score === null) {
    return null;
  }

  const level = getConfidenceLevel(score);
  const colorClass = CONFIDENCE_COLORS[level];
  const accessibleLabel = label ?? `${Math.round(score * 100)}% textual overlap`;

  return (
    <span
      className={cn(
        "inline-block h-2 w-2 shrink-0 rounded-full",
        colorClass,
        className
      )}
      title={`${accessibleLabel} — ${OVERLAP_DESCRIPTION}`}
      aria-label={accessibleLabel}
      aria-describedby={descriptionId}
      role="img"
    >
      <span id={descriptionId} className="sr-only">
        {OVERLAP_DESCRIPTION}
      </span>
    </span>
  );
}
