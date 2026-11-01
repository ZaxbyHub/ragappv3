import { beforeEach, describe, expect, it } from "vitest";

import type { SettingsResponse } from "@/lib/api";
import { useSettingsStore } from "./useSettingsStore";

function serverSettings(overrides: Partial<SettingsResponse> = {}): SettingsResponse {
  return {
    port: 9090,
    data_dir: "./data",
    ollama_embedding_url: "",
    ollama_chat_url: "",
    embedding_model: "",
    chat_model: "",
    chunk_size_chars: 2000,
    chunk_overlap_chars: 200,
    retrieval_top_k: 5,
    max_distance_threshold: 0.75,
    retrieval_window: 1,
    vector_metric: "cosine",
    embedding_doc_prefix: "",
    embedding_query_prefix: "",
    maintenance_mode: false,
    auto_scan_enabled: false,
    auto_scan_interval_minutes: 60,
    enable_model_validation: false,
    embedding_batch_size: 64,
    max_file_size_mb: 25,
    allowed_extensions: [".txt"],
    backend_cors_origins: [],
    initial_retrieval_top_k: 20,
    reranker_top_n: 5,
    ...overrides,
  };
}

describe("issue #773 settings validation", () => {
  beforeEach(() => {
    useSettingsStore.getState().resetState();
  });

  it("validateForm accepts an untouched server-valid value outside the old frontend band", () => {
    useSettingsStore.getState().initializeForm(
      serverSettings({
        max_distance_threshold: 1.5,
        retrieval_window: 4,
        initial_retrieval_top_k: 1,
        reranker_top_n: 21,
      }),
    );

    expect(useSettingsStore.getState().formData).toMatchObject({
      max_distance_threshold: 1.5,
      retrieval_window: 4,
      initial_retrieval_top_k: 1,
      reranker_top_n: 21,
    });
    expect(useSettingsStore.getState().validateForm()).toBe(true);
    expect(useSettingsStore.getState().formData).toMatchObject({
      max_distance_threshold: 1.5,
      retrieval_window: 4,
      initial_retrieval_top_k: 1,
      reranker_top_n: 21,
    });
    expect(useSettingsStore.getState().errors).toEqual({});
  });

  it("still rejects a value the operator edits outside the server range", () => {
    useSettingsStore.getState().initializeForm(serverSettings());
    useSettingsStore.getState().updateFormField("max_distance_threshold", -1);

    expect(useSettingsStore.getState().validateForm()).toBe(false);
    expect(useSettingsStore.getState().errors.max_distance_threshold).toBeTruthy();
  });

  it("keeps cross-field chunk overlap validation after hydration", () => {
    useSettingsStore.getState().initializeForm(serverSettings());
    useSettingsStore.getState().updateFormField("chunk_size_chars", 100);
    useSettingsStore.getState().updateFormField("chunk_overlap_chars", 100);

    expect(useSettingsStore.getState().validateForm()).toBe(false);
    expect(useSettingsStore.getState().errors.chunk_overlap_chars).toMatch(/less than chunk size/i);
  });
});
