// frontend/src/lib/evidence.ts
// Additive evidence-inspection contract (issue #508 / A2). Not a Source
// replacement: consumers derive a presentation-oriented view from an existing
// Source without changing the retrieval wire types.
import type { Source } from "@/lib/api";

/** Where the evidence physically lives, for preview targeting. */
export interface EvidenceLocation {
  kind: "pdf-page" | "section" | "artifact" | "unknown";
  pageNumber?: number;
  section?: string;
  description?: string;
}

/**
 * Whether the evidence's expanded context / original document is reachable
 * right now. `unavailableReason` distinguishes why the saved excerpt is shown
 * instead of live context.
 */
export interface EvidenceAvailability {
  state: "available" | "unavailable";
  unavailableReason?: "context-unavailable" | "original-unavailable" | "location-unknown";
}

export interface EvidenceView {
  location: EvidenceLocation;
  availability: EvidenceAvailability;
}

/**
 * Derive the evidence location from the source's page/section/artifact fields
 * (flattened `page_number` first, then `metadata.page_number`, then section,
 * then artifact identity), with a description fallback for artifact rows.
 * `availability` defaults to available; callers that know the expanded-context
 * fetch failed pass `{ state: "unavailable", unavailableReason: ... }`.
 */
export function buildEvidenceView(
  source: Source,
  availability: EvidenceAvailability = { state: "available" }
): EvidenceView {
  const metadata = source.metadata as Record<string, unknown> | undefined;
  const pageCandidate =
    typeof source.page_number === "number"
      ? source.page_number
      : typeof metadata?.page_number === "number"
        ? metadata.page_number
        : undefined;

  if (pageCandidate !== undefined) {
    return {
      location: {
        kind: "pdf-page",
        pageNumber: pageCandidate,
        section: source.section,
        description: source.description,
      },
      availability,
    };
  }

  if (source.section) {
    return {
      location: { kind: "section", section: source.section, description: source.description },
      availability,
    };
  }

  if (source.artifact_id) {
    return {
      location: {
        kind: "artifact",
        description: source.description ?? source.modality,
      },
      availability,
    };
  }

  return { location: { kind: "unknown" }, availability };
}

/**
 * Resolve whether a source label was cited in (and validated against) the
 * answer. Returns "unknown" when no label exists or no validation set was
 * supplied — never a fabricated cited/verified state.
 */
export function resolveCitedState(
  label: string | undefined,
  validLabels?: Set<string>
): "cited" | "retrieved" | "unknown" {
  if (!label || !label.trim()) return "unknown";
  if (!validLabels) return "unknown";
  return validLabels.has(label) ? "cited" : "retrieved";
}
