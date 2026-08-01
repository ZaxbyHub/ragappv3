import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DraftEvidence, DraftPaginated } from "@/lib/api/draftRoom";

const listDraftEvidenceMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    listDraftEvidence: listDraftEvidenceMock,
  };
});

import { DraftEvidencePanel } from "./DraftEvidencePanel";

function makeEvidence(overrides: Partial<DraftEvidence> = {}): DraftEvidence {
  return {
    id: 1,
    job_id: 5,
    label: "D1",
    source_kind: "draft_input",
    title: "Uploaded manuscript",
    passage: "The bridge opened to traffic in 1932.",
    passage_sha256: "a".repeat(64),
    source_content_sha256: "b".repeat(64),
    draft_input_id: 3,
    file_id: null,
    wiki_page_id: null,
    wiki_claim_id: null,
    kms_entry_id: null,
    chunk_uid: null,
    page_number: null,
    section: null,
    retrieval_score: null,
    authority: "primary",
    as_of_date: "2025-01-01",
    source_updated_at: "2025-06-01T00:00:00Z",
    source_deleted_at: null,
    source_deleted: false,
    ...overrides,
  };
}

function paginated<T>(items: T[]): DraftPaginated<T> {
  return { items, total: items.length, page: 1, per_page: 20 };
}

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

describe("DraftEvidencePanel", () => {
  afterEach(() => {
    cleanup();
    listDraftEvidenceMock.mockReset();
  });

  it("shows an honest empty state when no job has run yet", () => {
    render(<DraftEvidencePanel draftId={42} jobId={null} />, { wrapper });
    expect(screen.getByText(/no newsroom run has produced evidence yet/i)).toBeInTheDocument();
    expect(listDraftEvidenceMock).not.toHaveBeenCalled();
  });

  it("renders the required evidence card fields and the citation label", async () => {
    listDraftEvidenceMock.mockResolvedValue(paginated([makeEvidence()]));
    render(<DraftEvidencePanel draftId={42} jobId={5} />, { wrapper });

    await waitFor(() => expect(listDraftEvidenceMock).toHaveBeenCalledWith(42, { page: 1, per_page: 20, job_id: 5 }));
    expect(await screen.findByText("[D1]")).toBeInTheDocument();
    expect(screen.getByText("Uploaded manuscript")).toBeInTheDocument();
    expect(screen.getByText("Primary")).toBeInTheDocument();
    expect(screen.getByText("The bridge opened to traffic in 1932.")).toBeInTheDocument();
  });

  it("renders SOURCE_DELETED_WARNING and marks a deleted-source card non-reusable", async () => {
    listDraftEvidenceMock.mockResolvedValue(
      paginated([makeEvidence({ id: 2, source_deleted: true, source_deleted_at: "2026-01-01T00:00:00Z" })])
    );
    render(<DraftEvidencePanel draftId={42} jobId={5} />, { wrapper });

    expect(await screen.findByText("Source deleted after this revision")).toBeInTheDocument();
    expect(screen.getByText("Not reusable")).toBeInTheDocument();
    const card = screen.getByText("Not reusable").closest("li");
    expect(card?.className).toMatch(/opacity-70/);
  });
});
