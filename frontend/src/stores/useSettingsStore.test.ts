/**
 * PR B: settings store regression tests for the redesigned dirty/discard
 * snapshot logic and curator validation.
 *
 * Covers:
 *   - Initial dirty count is zero after initializeForm.
 *   - Editing a field flips dirtyFields to include exactly that key.
 *   - dirtyByTab correctly partitions across tabs (using FIELD_TAB).
 *   - discard restores the snapshot and zeroes dirty.
 *   - validateForm flags curator-enabled-without-url/model.
 *   - validateForm enforces curator numeric ranges.
 *   - hasChanges follows dirtyFields rather than the old cascade.
 */
import { describe, it, expect, beforeEach } from "vitest";
import {
  useSettingsStore,
  type SettingsFormData,
} from "./useSettingsStore";
import type { SettingsResponse } from "@/lib/api";

function baseSettings(): SettingsResponse {
  return {
    port: 9090,
    data_dir: "/tmp/data",
    ollama_embedding_url: "http://localhost:11434",
    ollama_chat_url: "http://localhost:11434",
    instant_chat_url: "http://localhost:1234",
    embedding_model: "harrier",
    chat_model: "llama3",
    instant_chat_model: "tinyllm",
    default_chat_mode: "thinking",
    instant_initial_retrieval_top_k: 10,
    instant_reranker_top_n: 4,
    instant_memory_context_top_k: 2,
    instant_max_tokens: 4096,
    chunk_size_chars: 2000,
    chunk_overlap_chars: 200,
    retrieval_top_k: 5,
    max_distance_threshold: 0.7,
    retrieval_window: 1,
    vector_metric: "cosine",
    embedding_doc_prefix: "Passage: ",
    embedding_query_prefix: "Query: ",
    maintenance_mode: false,
    auto_scan_enabled: false,
    auto_scan_interval_minutes: 60,
    enable_model_validation: false,
    embedding_batch_size: 64,
    reranking_enabled: false,
    reranker_url: "",
    reranker_model: "",
    initial_retrieval_top_k: 20,
    reranker_top_n: 5,
    hybrid_search_enabled: false,
    hybrid_alpha: 0.5,
    wiki_enabled: true,
    wiki_compile_on_ingest: true,
    wiki_compile_on_query: true,
    wiki_compile_after_indexing: true,
    wiki_lint_enabled: true,
    wiki_llm_curator_enabled: false,
    wiki_llm_curator_url: "",
    wiki_llm_curator_model: "",
    wiki_llm_curator_temperature: 0.0,
    wiki_llm_curator_max_input_chars: 6000,
    wiki_llm_curator_max_output_tokens: 2048,
    wiki_llm_curator_timeout_sec: 120,
    wiki_llm_curator_concurrency: 1,
    wiki_llm_curator_mode: "draft",
    wiki_llm_curator_require_quote_match: true,
    wiki_llm_curator_require_chunk_id: true,
    wiki_llm_curator_run_on_ingest: true,
    wiki_llm_curator_run_on_query: false,
    wiki_llm_curator_run_on_manual: true,
    effective_sources: { ollama_chat_url: "kv", instant_chat_model: "env" },
    max_file_size_mb: 100,
    allowed_extensions: [".pdf", ".txt"],
    backend_cors_origins: ["http://localhost:3000"],
  } as unknown as SettingsResponse;
}

beforeEach(() => {
  useSettingsStore.getState().resetState();
});

describe("useSettingsStore (PR B)", () => {
  it("initializes the snapshot so dirtyFields is empty after load", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    expect(useSettingsStore.getState().dirtyFields().size).toBe(0);
    expect(useSettingsStore.getState().hasChanges()).toBe(false);
  });

  it("editing a single field marks exactly that field dirty", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore.getState().updateFormField("retrieval_top_k", 17);
    const dirty = useSettingsStore.getState().dirtyFields();
    expect(dirty.size).toBe(1);
    expect(dirty.has("retrieval_top_k")).toBe(true);
    expect(useSettingsStore.getState().hasChanges()).toBe(true);
  });

  it("dirtyByTab partitions across tabs correctly", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore.getState().updateFormField("retrieval_top_k", 17); // retrieval
    useSettingsStore.getState().updateFormField("chat_model", "newmodel"); // models
    useSettingsStore
      .getState()
      .updateFormField("wiki_llm_curator_enabled", true); // wiki
    const tabs = useSettingsStore.getState().dirtyByTab();
    expect(tabs.retrieval).toBe(1);
    expect(tabs.models).toBe(1);
    expect(tabs.wiki).toBe(1);
    expect(tabs.documents).toBe(0);
    expect(tabs.maintenance).toBe(0);
    expect(tabs.overview).toBe(0);
  });

  it("tracks instant model settings as model-tab changes", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore
      .getState()
      .updateFormField("instant_chat_model", "faster-model");
    useSettingsStore.getState().updateFormField("default_chat_mode", "instant");
    useSettingsStore
      .getState()
      .updateFormField("instant_initial_retrieval_top_k", 8);
    const dirty = useSettingsStore.getState().dirtyFields();
    expect(dirty.has("instant_chat_model")).toBe(true);
    expect(dirty.has("default_chat_mode")).toBe(true);
    expect(dirty.has("instant_initial_retrieval_top_k")).toBe(true);
    expect(useSettingsStore.getState().dirtyByTab().models).toBe(3);
    expect(useSettingsStore.getState().dirtyByTab().retrieval).toBe(0);
  });

  it("discard restores the snapshot and clears dirty + errors", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore.getState().updateFormField("retrieval_top_k", 17);
    useSettingsStore.getState().updateFormField("chat_model", "newmodel");
    useSettingsStore.getState().setErrors({ retrieval_top_k: "bad" });
    expect(useSettingsStore.getState().hasChanges()).toBe(true);

    useSettingsStore.getState().discard();
    expect(useSettingsStore.getState().hasChanges()).toBe(false);
    expect(useSettingsStore.getState().dirtyFields().size).toBe(0);
    expect(useSettingsStore.getState().formData.retrieval_top_k).toBe(5);
    expect(useSettingsStore.getState().formData.chat_model).toBe("llama3");
    expect(useSettingsStore.getState().errors).toEqual({});
  });

  it("validateForm flags curator-enabled-without-url-or-model", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore
      .getState()
      .updateFormField("wiki_llm_curator_enabled", true);
    const ok = useSettingsStore.getState().validateForm();
    expect(ok).toBe(false);
    const errs = useSettingsStore.getState().errors;
    expect(errs.wiki_llm_curator_url).toBeTruthy();
    expect(errs.wiki_llm_curator_model).toBeTruthy();
  });

  it("validateForm passes when curator enabled with url + model", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore
      .getState()
      .updateFormField("wiki_llm_curator_enabled", true);
    useSettingsStore
      .getState()
      .updateFormField("wiki_llm_curator_url", "https://api.example.com");
    useSettingsStore
      .getState()
      .updateFormField("wiki_llm_curator_model", "qwen-1b");
    const ok = useSettingsStore.getState().validateForm();
    expect(ok).toBe(true);
  });

  it("validateForm rejects out-of-range curator numeric fields", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore
      .getState()
      .updateFormField("wiki_llm_curator_temperature", 2.5);
    useSettingsStore
      .getState()
      .updateFormField("wiki_llm_curator_max_input_chars", 100);
    useSettingsStore
      .getState()
      .updateFormField("wiki_llm_curator_concurrency", 99);
    useSettingsStore.getState().validateForm();
    const errs = useSettingsStore.getState().errors;
    expect(errs.wiki_llm_curator_temperature).toBeTruthy();
    expect(errs.wiki_llm_curator_max_input_chars).toBeTruthy();
    expect(errs.wiki_llm_curator_concurrency).toBeTruthy();
  });

  it("validateForm rejects unknown curator mode", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore
      .getState()
      .updateFormField(
        "wiki_llm_curator_mode" as keyof SettingsFormData,
        "yolo" as never,
      );
    const ok = useSettingsStore.getState().validateForm();
    expect(ok).toBe(false);
    expect(
      useSettingsStore.getState().errors.wiki_llm_curator_mode,
    ).toBeTruthy();
  });

  it("validateForm rejects invalid instant model settings", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore.getState().updateFormField("instant_chat_url", "localhost:1234");
    useSettingsStore.getState().updateFormField("instant_chat_model", "");
    useSettingsStore
      .getState()
      .updateFormField("default_chat_mode", "instant");
    useSettingsStore.getState().updateFormField("instant_max_tokens", 1.5);
    const ok = useSettingsStore.getState().validateForm();
    expect(ok).toBe(false);
    const errs = useSettingsStore.getState().errors;
    expect(errs.instant_chat_url).toBeTruthy();
    expect(errs.instant_chat_model).toBeTruthy();
    expect(errs.instant_max_tokens).toBeTruthy();
  });

  it("validateForm allows thinking mode without an instant model", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore.getState().updateFormField("instant_chat_model", "");
    const ok = useSettingsStore.getState().validateForm();
    expect(ok).toBe(true);
    expect(useSettingsStore.getState().errors.instant_chat_model).toBeUndefined();
  });

  it("validateForm rejects unknown default chat mode", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore
      .getState()
      .updateFormField("default_chat_mode", "turbo" as never);
    const ok = useSettingsStore.getState().validateForm();
    expect(ok).toBe(false);
    expect(useSettingsStore.getState().errors.default_chat_mode).toBeTruthy();
  });

  it("save snapshot resync via initializeForm zeroes dirty after persist round-trip", () => {
    useSettingsStore.getState().initializeForm(baseSettings());
    useSettingsStore.getState().updateFormField("retrieval_top_k", 11);
    expect(useSettingsStore.getState().hasChanges()).toBe(true);
    // Simulate the post-save flow: SettingsPage calls initializeForm with
    // the freshly-persisted settings.
    const persisted = { ...baseSettings(), retrieval_top_k: 11 };
    useSettingsStore.getState().initializeForm(persisted);
    expect(useSettingsStore.getState().hasChanges()).toBe(false);
  });
});
