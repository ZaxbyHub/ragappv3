import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ReactNode } from "react";
import type { SettingsFormData } from "@/stores/useSettingsStore";
import { RAGSettings } from "./RAGSettings";

vi.mock("@/components/ui/select", async () => {
  return {
    Select: ({ children }: { children: ReactNode }) => (
      <div data-testid="vector-select">{children}</div>
    ),
    SelectContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
      <div data-value={value}>{children}</div>
    ),
    SelectTrigger: ({ id, children }: { id?: string; children: ReactNode }) => (
      <div id={id}>{children}</div>
    ),
    SelectValue: () => null,
  };
});

const formData = {
  max_distance_threshold: 0.7,
  retrieval_window: 1,
  vector_metric: "cosine",
  embedding_batch_size: 64,
} as SettingsFormData;

describe("issue #773 RAG settings controls", () => {
  it("offers only LanceDB-accepted vector metrics", () => {
    render(<RAGSettings formData={formData} errors={{}} onChange={vi.fn()} />);

    const accepted = new Set(["l2", "cosine", "dot", "hamming"]);
    const values = Array.from(
      screen.getByTestId("vector-select").querySelectorAll<HTMLElement>("[data-value]"),
    ).map((item) => item.dataset.value);

    expect(values.filter((value) => !accepted.has(value ?? "")).length).toBe(0);
    expect(values).toEqual(expect.arrayContaining(["cosine"]));
  });

  it("bounds the embedding batch size input to the backend range", () => {
    render(<RAGSettings formData={formData} errors={{}} onChange={vi.fn()} />);

    expect(screen.getByLabelText("Embedding Batch Size")).toHaveAttribute("max", "128");
  });
});
