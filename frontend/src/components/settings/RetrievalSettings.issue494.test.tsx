// Regression check for issue #494 AC25 / UI-031: `formData.hybrid_alpha ||
// 0.5` (and the sibling `initial_retrieval_top_k || 20`,
// `reranker_top_n || 5`) coerces the legitimate value 0 to the fallback,
// so a saved zero silently renders as 0.5 / 20 / 5. This check asserts
// REQUIRED behavior that does not exist at the pre-fix base (a543361) and
// is expected to FAIL there; it prints an "AC25 CHECK: FAIL" sentinel
// immediately before the discriminating assertion. Mirrors the harness of
// RetrievalSettings.test.tsx (minimal formData shape cast).
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { RetrievalSettings } from "./RetrievalSettings";
import type { SettingsFormData, SettingsErrors } from "@/stores/useSettingsStore";

describe("RetrievalSettings zero-value rendering (issue #494)", () => {
  const baseFormData: SettingsFormData = {
    // Minimal shape — only the fields this component reads are required.
    hybrid_alpha: 0.5,
  } as Partial<SettingsFormData> as SettingsFormData;
  const baseErrors: SettingsErrors = {};

  it("AC25: zero renders as \"0\" in the hybrid alpha input + slider, initial retrieval top-k, and reranker top-n — not as the || fallbacks", () => {
    render(
      <RetrievalSettings
        formData={{
          ...baseFormData,
          hybrid_alpha: 0,
          initial_retrieval_top_k: 0,
          reranker_top_n: 0,
        }}
        errors={baseErrors}
        onChange={vi.fn()}
      />,
    );

    console.log("AC25 CHECK: FAIL");

    // Hybrid alpha: BOTH the numeric input (labeled via <Label htmlFor>) and
    // the range slider (aria-label) must show 0, not the 0.5 fallback.
    const alphaNumber = screen.getByLabelText("Hybrid Alpha", {
      selector: "input[type='number']",
    }) as HTMLInputElement;
    expect(alphaNumber.value).toBe("0");

    const alphaSlider = screen.getByRole("slider", {
      name: "Hybrid Alpha",
    }) as HTMLInputElement;
    expect(alphaSlider.value).toBe("0");

    // Siblings: same falsy-zero bug class (`|| 20`, `|| 5`).
    const initialTopK = screen.getByLabelText(
      "Initial Retrieval Top-K",
    ) as HTMLInputElement;
    expect(initialTopK.value).toBe("0");

    const rerankerTopN = screen.getByLabelText("Reranker Top-N") as HTMLInputElement;
    expect(rerankerTopN.value).toBe("0");
  });
});
