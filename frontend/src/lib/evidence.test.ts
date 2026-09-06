import { describe, expect, it } from "vitest";
import { buildEvidenceView, resolveCitedState } from "@/lib/evidence";
import type { Source } from "@/lib/api";

const baseSource: Source = {
  id: "s1",
  filename: "doc.pdf",
};

describe("buildEvidenceView", () => {
  it("derives a pdf-page location from the flattened page_number", () => {
    const view = buildEvidenceView({ ...baseSource, page_number: 4 });
    expect(view.location).toEqual({
      kind: "pdf-page",
      pageNumber: 4,
      section: undefined,
      description: undefined,
    });
    expect(view.availability).toEqual({ state: "available" });
  });

  it("falls back to metadata.page_number when the flattened field is absent", () => {
    const view = buildEvidenceView({
      ...baseSource,
      metadata: { page_number: 9 },
    });
    expect(view.location.kind).toBe("pdf-page");
    expect(view.location.pageNumber).toBe(9);
  });

  it("keeps the section alongside a page location", () => {
    const view = buildEvidenceView({
      ...baseSource,
      page_number: 2,
      section: "Results",
    });
    expect(view.location.kind).toBe("pdf-page");
    expect(view.location.section).toBe("Results");
  });

  it("derives a section location when no page exists", () => {
    const view = buildEvidenceView({ ...baseSource, section: "Methods" });
    expect(view.location).toEqual({
      kind: "section",
      section: "Methods",
      description: undefined,
    });
  });

  it("derives an artifact location with the description, falling back to modality", () => {
    const withDescription = buildEvidenceView({
      ...baseSource,
      artifact_id: "atom-1",
      modality: "chart",
      description: "Revenue by quarter",
    });
    expect(withDescription.location).toEqual({
      kind: "artifact",
      description: "Revenue by quarter",
    });

    const withoutDescription = buildEvidenceView({
      ...baseSource,
      artifact_id: "atom-2",
      modality: "image",
    });
    expect(withoutDescription.location).toEqual({
      kind: "artifact",
      description: "image",
    });
  });

  it("returns an unknown location when nothing derivable exists", () => {
    const view = buildEvidenceView({ ...baseSource });
    expect(view.location).toEqual({ kind: "unknown" });
  });

  it("carries the caller-supplied unavailability through unchanged", () => {
    const view = buildEvidenceView(
      { ...baseSource, page_number: 4 },
      { state: "unavailable", unavailableReason: "context-unavailable" }
    );
    expect(view.availability).toEqual({
      state: "unavailable",
      unavailableReason: "context-unavailable",
    });
    expect(view.location.kind).toBe("pdf-page");
  });
});

describe("resolveCitedState", () => {
  const valid = new Set(["S1", "S3"]);

  it("returns cited for a label in the valid set", () => {
    expect(resolveCitedState("S1", valid)).toBe("cited");
  });

  it("returns retrieved for a label outside the valid set", () => {
    expect(resolveCitedState("S2", valid)).toBe("retrieved");
  });

  it("returns unknown when no validation set is available", () => {
    expect(resolveCitedState("S1", undefined)).toBe("unknown");
  });

  it("returns unknown for absent or blank labels", () => {
    expect(resolveCitedState(undefined, valid)).toBe("unknown");
    expect(resolveCitedState("", valid)).toBe("unknown");
    expect(resolveCitedState("   ", valid)).toBe("unknown");
  });
});
