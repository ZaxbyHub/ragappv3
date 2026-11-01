import { afterEach, describe, expect, it } from "vitest";

import type { SettingsResponse } from "@/lib/api";
import { useSettingsStore } from "./useSettingsStore";

describe("issue #773 settings metric hydration", () => {
  afterEach(() => {
    useSettingsStore.getState().resetState();
  });

  it.each([
    ["euclidean", "l2"],
    ["dot_product", "dot"],
  ])("canonicalizes the persisted %s alias to %s", (persisted, canonical) => {
    useSettingsStore.getState().initializeForm({
      vector_metric: JSON.stringify(persisted),
    } as SettingsResponse);

    expect(useSettingsStore.getState().formData.vector_metric).toBe(canonical);
  });

  it("uses field-specific integer messages for fractional backend numeric fields", () => {
    useSettingsStore.getState().initializeForm({
      chunk_size_chars: 2000,
      chunk_overlap_chars: 200,
      embedding_batch_size: 64,
      max_distance_threshold: 0.75,
      retrieval_window: 1,
      initial_retrieval_top_k: 20,
      reranker_top_n: 5,
    } as SettingsResponse);

    useSettingsStore.getState().updateFormField("embedding_batch_size", Number.NaN);
    useSettingsStore.getState().updateFormField("retrieval_window", 1.5);
    useSettingsStore.getState().updateFormField("initial_retrieval_top_k", 1.5);
    useSettingsStore.getState().updateFormField("reranker_top_n", 1.5);
    useSettingsStore.getState().updateFormField("max_distance_threshold", Number.POSITIVE_INFINITY);

    expect(useSettingsStore.getState().validateForm()).toBe(false);
    expect(useSettingsStore.getState().errors.embedding_batch_size).toBeTruthy();
    expect(useSettingsStore.getState().errors.retrieval_window).toBe(
      "Retrieval window must be a non-negative integer"
    );
    expect(useSettingsStore.getState().errors.initial_retrieval_top_k).toBe(
      "Initial retrieval top-k must be a positive integer"
    );
    expect(useSettingsStore.getState().errors.reranker_top_n).toBe("Reranker top-n must be a positive integer");
    expect(useSettingsStore.getState().errors.max_distance_threshold).toBeTruthy();
  });
});
