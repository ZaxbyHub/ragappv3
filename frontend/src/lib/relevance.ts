export type ScoreType = "distance" | "rerank";

export interface RelevanceLabel {
  text: string;
  color: string; // Tailwind text color class
}

/**
 * Returns a descriptive label and color for a relevance score based on score_type and value.
 *
 * Score contract (issue #36, calibrated 2026-09 against the deployed
 * harrier-oss-v1-0.6b + bge-reranker-v2-m3 stack; dataset and derivation:
 * backend/tests/eval/calibration_2026_09/):
 * - distance: cosine distance, 0=identical, higher=worse. Bands derived from
 *   the gold/no-answer query distributions: Highly Relevant <= 0.56 (q25 of
 *   gold results), Relevant <= 0.67 (q75 of gold), Related <= 0.77 (q25 of
 *   no-answer results); beyond that the backend's max_distance_threshold
 *   (0.75) filters the tail on non-rerank paths.
 * - rerank: sigmoid of the reranker's raw logit (#511 contract), 0-1, higher
 *   = better. Bands kept at 0.7/0.4/0.2 — held-out agreement tied-best with
 *   the best data-derived candidate (85.0% vs 84.1%); the Tangential floor
 *   matches the no-answer distribution's q95 (0.181).
 * The backend chat pipeline emits only "distance" or "rerank" for sources;
 * the memory channel carries its own score_type vocabulary (rrf/fts/dense)
 * which is rendered without relevance labels. Unknown or missing score_type
 * falls back to distance semantics.
 */
export function getRelevanceLabel(score: number, scoreType?: ScoreType): RelevanceLabel {
  if (!scoreType || scoreType === "distance") {
    // Distance: 0=identical; calibrated bands (higher = less relevant)
    if (score <= 0.56) return { text: "Highly Relevant", color: "text-success" };
    if (score <= 0.67) return { text: "Relevant", color: "text-success-subdued" };
    if (score <= 0.77) return { text: "Related", color: "text-warning" };
    return { text: "Tangential", color: "text-destructive" };
  }
  // Rerank: sigmoid(logit), 0-1, higher=better
  if (score >= 0.7) return { text: "Highly Relevant", color: "text-success" };
  if (score >= 0.4) return { text: "Relevant", color: "text-success-subdued" };
  if (score >= 0.2) return { text: "Related", color: "text-warning" };
  return { text: "Tangential", color: "text-destructive" };
}
