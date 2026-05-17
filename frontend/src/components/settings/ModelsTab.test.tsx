import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ModelsTab } from "./ModelsTab";
import type { SettingsFormData } from "@/stores/useSettingsStore";

function formData(): SettingsFormData {
  return {
    chunk_size_chars: 2000,
    chunk_overlap_chars: 200,
    retrieval_top_k: 5,
    auto_scan_enabled: false,
    auto_scan_interval_minutes: 60,
    max_distance_threshold: 0.7,
    retrieval_window: 1,
    vector_metric: "cosine",
    embedding_doc_prefix: "",
    embedding_query_prefix: "",
    embedding_batch_size: 64,
    reranking_enabled: false,
    reranker_url: "",
    reranker_model: "BAAI/bge-reranker-v2-m3",
    initial_retrieval_top_k: 20,
    reranker_top_n: 5,
    hybrid_search_enabled: true,
    hybrid_alpha: 0.5,
    ollama_embedding_url: "http://embed.local",
    ollama_chat_url: "http://thinking.local",
    embedding_model: "harrier",
    chat_model: "thinking-model",
    instant_chat_url: "http://instant.local",
    instant_chat_model: "instant-model",
    default_chat_mode: "thinking",
    instant_initial_retrieval_top_k: 10,
    instant_reranker_top_n: 4,
    instant_memory_context_top_k: 2,
    instant_max_tokens: 4096,
    wiki_enabled: true,
    wiki_compile_on_ingest: true,
    wiki_compile_on_query: true,
    wiki_compile_after_indexing: true,
    wiki_lint_enabled: true,
    wiki_llm_curator_enabled: false,
    wiki_llm_curator_url: "",
    wiki_llm_curator_model: "",
    wiki_llm_curator_temperature: 0,
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
    maintenance_mode: false,
  };
}

describe("ModelsTab", () => {
  it("renders thinking and instant model controls", () => {
    render(
      <ModelsTab
        formData={formData()}
        errors={{}}
        onChange={vi.fn()}
        effectiveSources={{
          chat_model: "kv",
          instant_chat_model: "env",
          default_chat_mode: "default",
        }}
      />,
    );

    expect(screen.getByLabelText(/Thinking chat service URL/i)).toHaveValue(
      "http://thinking.local",
    );
    expect(screen.getByLabelText(/Instant chat service URL/i)).toHaveValue(
      "http://instant.local",
    );
    expect(screen.getByLabelText(/Thinking chat model/i)).toHaveValue(
      "thinking-model",
    );
    expect(screen.getByLabelText(/Instant chat model/i)).toHaveValue(
      "instant-model",
    );
    expect(screen.getByRole("radio", { name: "Thinking" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(
      screen.getByRole("radiogroup", { name: /Default chat mode/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Instant mode tuning/i)).toBeInTheDocument();
  });

  it("emits changes for instant fields and default mode", () => {
    const onChange = vi.fn();
    render(
      <ModelsTab
        formData={formData()}
        errors={{}}
        onChange={onChange}
        effectiveSources={{}}
      />,
    );

    fireEvent.change(screen.getByLabelText(/Instant chat model/i), {
      target: { value: "nano" },
    });
    fireEvent.click(screen.getByRole("radio", { name: "Instant" }));

    expect(onChange).toHaveBeenCalledWith("instant_chat_model", "nano");
    expect(onChange).toHaveBeenCalledWith("default_chat_mode", "instant");
  });

  it("keeps the previous numeric value while a number field is cleared", () => {
    const onChange = vi.fn();
    render(
      <ModelsTab
        formData={formData()}
        errors={{}}
        onChange={onChange}
        effectiveSources={{}}
      />,
    );

    const input = screen.getByLabelText(/Max output tokens/i);

    fireEvent.change(input, {
      target: { value: "" },
    });

    expect(input).toHaveValue(null);
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.blur(input);
    expect(input).toHaveValue(4096);
  });
});
