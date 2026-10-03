import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SettingsFormData } from "@/stores/useSettingsStore";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { RetrievalSettings } from "./RetrievalSettings";

const baseFormData = {
  reranking_enabled: true,
  reranker_url: "",
  reranker_model: "",
  initial_retrieval_top_k: 20,
  reranker_top_n: 5,
  hybrid_search_enabled: false,
  hybrid_alpha: 0.5,
} as Partial<SettingsFormData> as SettingsFormData;

describe("issue #773 retrieval numeric controls", () => {
  afterEach(() => {
    useSettingsStore.getState().resetState();
  });

  it("accepts server-valid values outside the old arbitrary UI maxima", () => {
    render(
      <RetrievalSettings
        formData={{
          ...baseFormData,
          initial_retrieval_top_k: 1,
          reranker_top_n: 21,
        }}
        errors={{}}
        onChange={vi.fn()}
      />,
    );

    const initial = screen.getByLabelText("Initial Retrieval Top-K") as HTMLInputElement;
    const reranker = screen.getByLabelText("Reranker Top-N") as HTMLInputElement;

    expect(initial).toHaveAttribute("min", "1");
    expect(initial).not.toHaveAttribute("max");
    expect(initial.value).toBe("1");
    expect(initial.checkValidity()).toBe(true);
    expect(initial.validity.rangeUnderflow).toBe(false);
    expect(initial.validity.rangeOverflow).toBe(false);

    expect(reranker).toHaveAttribute("min", "1");
    expect(reranker).not.toHaveAttribute("max");
    expect(reranker.value).toBe("21");
    expect(reranker.checkValidity()).toBe(true);
    expect(reranker.validity.rangeUnderflow).toBe(false);
    expect(reranker.validity.rangeOverflow).toBe(false);
  });

  it.each([
    ["initial_retrieval_top_k", Number.NaN],
    ["initial_retrieval_top_k", 0],
    ["initial_retrieval_top_k", 1.5],
    ["reranker_top_n", Number.POSITIVE_INFINITY],
    ["reranker_top_n", 0],
    ["reranker_top_n", 1.5],
  ] as const)("store rejects edited %s value %s", (field, value) => {
    useSettingsStore.getState().updateFormField(field, value);

    expect(useSettingsStore.getState().validateForm()).toBe(false);
    expect(useSettingsStore.getState().errors[field]).toBeTruthy();
  });
});
