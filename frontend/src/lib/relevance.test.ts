import { describe, it, expect } from "vitest";
import { getRelevanceLabel, type ScoreType } from "./relevance";

/**
 * Calibrated relevance bands (2026-09 calibration, issue #36).
 * Spec: backend/tests/eval/calibration_2026_09/ (dataset, score_dump, analysis).
 *
 * distance (cosine, lower = better):
 *   Highly Relevant <= 0.56, Relevant <= 0.67, Related <= 0.77, else Tangential
 * rerank (sigmoid, higher = better):
 *   Highly Relevant >= 0.7, Relevant >= 0.4, Related >= 0.2, else Tangential
 *
 * Boundaries are inclusive on the "better" side. The "rrf" score type no
 * longer exists (backend produces only "distance" | "rerank").
 */
describe("getRelevanceLabel", () => {
  // ============================================
  // Distance score type (0=identical, higher=worse; calibrated 2026-09)
  // ============================================
  describe("distance score type", () => {
    it("test_distance_highly_relevant — getRelevanceLabel(0.3, 'distance').text === 'Highly Relevant'", () => {
      const result = getRelevanceLabel(0.3, "distance");
      expect(result.text).toBe("Highly Relevant");
      expect(result.color).toBe("text-success");
    });

    it("test_distance_relevant — getRelevanceLabel(0.6, 'distance').text === 'Relevant'", () => {
      const result = getRelevanceLabel(0.6, "distance");
      expect(result.text).toBe("Relevant");
      expect(result.color).toBe("text-success-subdued");
    });

    it("test_distance_related — getRelevanceLabel(0.7, 'distance').text === 'Related'", () => {
      const result = getRelevanceLabel(0.7, "distance");
      expect(result.text).toBe("Related");
      expect(result.color).toBe("text-warning");
    });

    it("test_distance_tangential — getRelevanceLabel(0.9, 'distance').text === 'Tangential'", () => {
      const result = getRelevanceLabel(0.9, "distance");
      expect(result.text).toBe("Tangential");
      expect(result.color).toBe("text-destructive");
    });

    it("test_distance_boundary_056 — getRelevanceLabel(0.56, 'distance').text === 'Highly Relevant' (inclusive, better side)", () => {
      const result = getRelevanceLabel(0.56, "distance");
      expect(result.text).toBe("Highly Relevant");
    });

    it("test_distance_boundary_05601 — getRelevanceLabel(0.5601, 'distance').text === 'Relevant'", () => {
      const result = getRelevanceLabel(0.5601, "distance");
      expect(result.text).toBe("Relevant");
    });

    it("test_distance_boundary_067 — getRelevanceLabel(0.67, 'distance').text === 'Relevant' (inclusive, better side)", () => {
      const result = getRelevanceLabel(0.67, "distance");
      expect(result.text).toBe("Relevant");
    });

    it("test_distance_boundary_06701 — getRelevanceLabel(0.6701, 'distance').text === 'Related'", () => {
      const result = getRelevanceLabel(0.6701, "distance");
      expect(result.text).toBe("Related");
    });

    it("test_distance_boundary_077 — getRelevanceLabel(0.77, 'distance').text === 'Related' (inclusive, better side)", () => {
      const result = getRelevanceLabel(0.77, "distance");
      expect(result.text).toBe("Related");
    });

    it("test_distance_boundary_07701 — getRelevanceLabel(0.7701, 'distance').text === 'Tangential'", () => {
      const result = getRelevanceLabel(0.7701, "distance");
      expect(result.text).toBe("Tangential");
    });
  });

  // ============================================
  // Rerank score type (0-1, higher=better; calibrated 2026-09)
  // ============================================
  describe("rerank score type", () => {
    it("test_rerank_highly_relevant — getRelevanceLabel(0.85, 'rerank').text === 'Highly Relevant'", () => {
      const result = getRelevanceLabel(0.85, "rerank");
      expect(result.text).toBe("Highly Relevant");
      expect(result.color).toBe("text-success");
    });

    it("test_rerank_relevant — getRelevanceLabel(0.5, 'rerank').text === 'Relevant'", () => {
      const result = getRelevanceLabel(0.5, "rerank");
      expect(result.text).toBe("Relevant");
      expect(result.color).toBe("text-success-subdued");
    });

    it("test_rerank_related — getRelevanceLabel(0.3, 'rerank').text === 'Related'", () => {
      const result = getRelevanceLabel(0.3, "rerank");
      expect(result.text).toBe("Related");
      expect(result.color).toBe("text-warning");
    });

    it("test_rerank_tangential — getRelevanceLabel(0.05, 'rerank').text === 'Tangential'", () => {
      const result = getRelevanceLabel(0.05, "rerank");
      expect(result.text).toBe("Tangential");
      expect(result.color).toBe("text-destructive");
    });

    it("test_rerank_boundary_07 — getRelevanceLabel(0.7, 'rerank').text === 'Highly Relevant' (inclusive, better side)", () => {
      const result = getRelevanceLabel(0.7, "rerank");
      expect(result.text).toBe("Highly Relevant");
    });

    it("test_rerank_boundary_06999 — getRelevanceLabel(0.6999, 'rerank').text === 'Relevant'", () => {
      const result = getRelevanceLabel(0.6999, "rerank");
      expect(result.text).toBe("Relevant");
    });

    it("test_rerank_boundary_04 — getRelevanceLabel(0.4, 'rerank').text === 'Relevant' (inclusive, better side)", () => {
      const result = getRelevanceLabel(0.4, "rerank");
      expect(result.text).toBe("Relevant");
    });

    it("test_rerank_boundary_03999 — getRelevanceLabel(0.3999, 'rerank').text === 'Related'", () => {
      const result = getRelevanceLabel(0.3999, "rerank");
      expect(result.text).toBe("Related");
    });

    it("test_rerank_boundary_02 — getRelevanceLabel(0.2, 'rerank').text === 'Related' (inclusive, better side)", () => {
      const result = getRelevanceLabel(0.2, "rerank");
      expect(result.text).toBe("Related");
    });

    it("test_rerank_boundary_01999 — getRelevanceLabel(0.1999, 'rerank').text === 'Tangential'", () => {
      const result = getRelevanceLabel(0.1999, "rerank");
      expect(result.text).toBe("Tangential");
    });
  });

  // ============================================
  // Default behavior tests (unknown/undefined scoreType = distance semantics)
  // ============================================
  describe("default behavior", () => {
    it("test_undefined_score_type_defaults_to_distance — getRelevanceLabel(0.56, undefined).text === 'Highly Relevant'", () => {
      const result = getRelevanceLabel(0.56, undefined);
      expect(result.text).toBe("Highly Relevant");
    });

    it("test_undefined_score_type_distance_bands — undefined scoreType follows the distance bands at every boundary", () => {
      expect(getRelevanceLabel(0.5601, undefined).text).toBe("Relevant");
      expect(getRelevanceLabel(0.67, undefined).text).toBe("Relevant");
      expect(getRelevanceLabel(0.6701, undefined).text).toBe("Related");
      expect(getRelevanceLabel(0.77, undefined).text).toBe("Related");
      expect(getRelevanceLabel(0.7701, undefined).text).toBe("Tangential");
    });

    it("test_unknown_score_type_falls_back_to_distance — getRelevanceLabel(0.62, 'nonsense' as unknown as ScoreType).text === 'Relevant'", () => {
      const result = getRelevanceLabel(0.62, "nonsense" as unknown as ScoreType);
      expect(result.text).toBe("Relevant");
    });
  });

  // ============================================
  // Label structure tests
  // ============================================
  describe("label structure", () => {
    it("test_all_labels_have_color — Every label result should have a non-empty color string starting with 'text-'", () => {
      const testCases: Array<{ score: number; scoreType: ScoreType | undefined }> = [
        { score: 0.3, scoreType: "distance" },
        { score: 0.6, scoreType: "distance" },
        { score: 0.7, scoreType: "distance" },
        { score: 0.9, scoreType: "distance" },
        { score: 0.85, scoreType: "rerank" },
        { score: 0.5, scoreType: "rerank" },
        { score: 0.3, scoreType: "rerank" },
        { score: 0.05, scoreType: "rerank" },
        { score: 0.62, scoreType: undefined },
      ];

      testCases.forEach(({ score, scoreType }) => {
        const result = getRelevanceLabel(score, scoreType);
        expect(result.color).toBeTruthy();
        expect(result.color.startsWith("text-")).toBe(true);
      });
    });

    it("test_all_labels_have_text — Every label result should have non-empty text", () => {
      const testCases: Array<{ score: number; scoreType: ScoreType | undefined }> = [
        { score: 0.3, scoreType: "distance" },
        { score: 0.6, scoreType: "distance" },
        { score: 0.7, scoreType: "distance" },
        { score: 0.9, scoreType: "distance" },
        { score: 0.85, scoreType: "rerank" },
        { score: 0.5, scoreType: "rerank" },
        { score: 0.3, scoreType: "rerank" },
        { score: 0.05, scoreType: "rerank" },
        { score: 0.62, scoreType: undefined },
      ];

      testCases.forEach(({ score, scoreType }) => {
        const result = getRelevanceLabel(score, scoreType);
        expect(result.text).toBeTruthy();
        expect(result.text.length).toBeGreaterThan(0);
      });
    });
  });
});
