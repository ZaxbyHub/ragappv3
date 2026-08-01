import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  DraftClaim,
  DraftClaimStatus,
  DraftEvidence,
  DraftPaginated,
  DraftRevisionSummary,
} from "@/lib/api/draftRoom";
import { DRAFT_CLAIM_STATUSES } from "@/lib/api/draftRoom";

const listDraftClaimsMock = vi.hoisted(() => vi.fn());
const listDraftEvidenceMock = vi.hoisted(() => vi.fn());
const listDraftRevisionsMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    listDraftClaims: listDraftClaimsMock,
    listDraftEvidence: listDraftEvidenceMock,
    listDraftRevisions: listDraftRevisionsMock,
  };
});

import { DraftClaimsPanel } from "./DraftClaimsPanel";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";

function makeClaim(overrides: Partial<DraftClaim> = {}): DraftClaim {
  return {
    id: 1,
    revision_id: 7,
    ordinal: 1,
    claim_text: "The bridge opened in 1932.",
    claim_sha256: "a".repeat(64),
    span_start: 0,
    span_end: 10,
    claim_type: "factual",
    status: "supported",
    severity: "info",
    rationale: "Matched a vault passage.",
    retrieval_audit: null,
    resolution: "open",
    resolved_by: null,
    resolved_at: null,
    resolution_note: null,
    sources: [],
    ...overrides,
  };
}

function makeEvidence(overrides: Partial<DraftEvidence> = {}): DraftEvidence {
  return {
    id: 1,
    job_id: 5,
    label: "S1",
    source_kind: "document",
    title: "City Records 1932",
    passage: "The bridge opened to traffic in 1932.",
    passage_sha256: "b".repeat(64),
    source_content_sha256: "c".repeat(64),
    draft_input_id: null,
    file_id: 10,
    wiki_page_id: null,
    wiki_claim_id: null,
    kms_entry_id: null,
    chunk_uid: "chunk-1",
    page_number: null,
    section: null,
    retrieval_score: 0.9,
    authority: "official",
    as_of_date: null,
    source_updated_at: null,
    source_deleted_at: null,
    source_deleted: false,
    ...overrides,
  };
}

function makeRevisionSummary(overrides: Partial<DraftRevisionSummary> = {}): DraftRevisionSummary {
  return {
    id: 7,
    revision_no: 1,
    parent_revision_id: null,
    job_id: 5,
    source: "pipeline",
    content_sha256: "a".repeat(64),
    fact_status: "passed",
    is_current: true,
    created_by: 1,
    created_at: "2026-01-01T00:00:00Z",
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

describe("DraftClaimsPanel", () => {
  beforeEach(() => {
    // Every test's claims/evidence live on revision 7, produced by job 5 —
    // override per test only when the job-resolution behavior itself is
    // under test.
    listDraftRevisionsMock.mockResolvedValue(paginated([makeRevisionSummary()]));
  });

  afterEach(() => {
    cleanup();
    listDraftClaimsMock.mockReset();
    listDraftEvidenceMock.mockReset();
    listDraftRevisionsMock.mockReset();
    useDraftRoomUiStore.getState().resetForDraft(0);
  });

  it("filters across all six claim statuses", async () => {
    listDraftClaimsMock.mockResolvedValue(paginated([]));
    listDraftEvidenceMock.mockResolvedValue(paginated([]));
    render(<DraftClaimsPanel draftId={42} revisionId={7} />, { wrapper });

    await waitFor(() => expect(listDraftClaimsMock).toHaveBeenCalled());

    expect(DRAFT_CLAIM_STATUSES).toHaveLength(6);
    for (const status of DRAFT_CLAIM_STATUSES) {
      listDraftClaimsMock.mockClear();
      const label =
        status === "supported"
          ? "Supported"
          : status === "contradicted"
            ? "Contradicted"
            : status === "ambiguous"
              ? "Ambiguous"
              : status === "stale"
                ? "Stale"
                : status === "unsupported"
                  ? "Unsupported"
                  : "Opinion";
      fireEvent.click(screen.getByRole("button", { name: label }));
      await waitFor(() =>
        expect(listDraftClaimsMock).toHaveBeenCalledWith(
          42,
          expect.objectContaining({ status } as Partial<{ status: DraftClaimStatus }>)
        )
      );
    }
  });

  it("separates opinions from factual claims visually and in the accessible text", async () => {
    listDraftClaimsMock.mockResolvedValue(
      paginated([
        makeClaim({ id: 1, status: "supported", claim_type: "factual" }),
        makeClaim({ id: 2, status: "opinion", claim_type: "opinion", claim_text: "This is a bold choice." }),
      ])
    );
    listDraftEvidenceMock.mockResolvedValue(paginated([]));
    render(<DraftClaimsPanel draftId={42} revisionId={7} />, { wrapper });

    const factualHeading = await screen.findByRole("heading", { name: "Factual and quote claims" });
    const opinionsHeading = await screen.findByRole("heading", { name: "Opinions" });
    expect(factualHeading).toBeInTheDocument();
    expect(opinionsHeading).toBeInTheDocument();

    // The opinion claim's accessible text carries an explicit "Opinion" marker.
    expect(screen.getByText("This is a bold choice.").closest("li")?.textContent).toMatch(/Opinion/);
  });

  it("renders lexicalOverlapText and never a forbidden confidence/support/entailment term", async () => {
    listDraftClaimsMock.mockResolvedValue(
      paginated([
        makeClaim({
          id: 1,
          status: "supported",
          sources: [
            {
              id: 1,
              claim_id: 1,
              evidence_id: 1,
              relationship: "supports",
              exact_quote: "opened to traffic in 1932",
              passage_start: 0,
              passage_end: 20,
              lexical_overlap_score: 0.42,
            },
          ],
        }),
      ])
    );
    listDraftEvidenceMock.mockResolvedValue(paginated([makeEvidence()]));
    render(<DraftClaimsPanel draftId={42} revisionId={7} />, { wrapper });

    expect(await screen.findByText("Citation lexical overlap: 0.42")).toBeInTheDocument();

    const forbidden = /confidence|verified|entailment|support probability|\bprobability\b/i;
    expect(document.body.textContent ?? "").not.toMatch(forbidden);
  });

  it("renders SUPPORTED_BY_EVIDENCE for a supported claim, never a truth claim", async () => {
    listDraftClaimsMock.mockResolvedValue(paginated([makeClaim({ id: 1, status: "supported" })]));
    listDraftEvidenceMock.mockResolvedValue(paginated([]));
    render(<DraftClaimsPanel draftId={42} revisionId={7} />, { wrapper });

    expect(await screen.findByText("Supported by captured evidence")).toBeInTheDocument();
    const truthClaimPattern = /verified true|factually true|human-written|published/i;
    expect(document.body.textContent ?? "").not.toMatch(truthClaimPattern);
  });

  it("offers Edit draft and View retrieval audit for unsupported/blocker rows only", async () => {
    listDraftClaimsMock.mockResolvedValue(
      paginated([
        makeClaim({ id: 1, status: "supported" }),
        makeClaim({ id: 2, status: "unsupported", retrieval_audit: { normalized_query: "bridge opening" } }),
      ])
    );
    listDraftEvidenceMock.mockResolvedValue(paginated([]));
    render(<DraftClaimsPanel draftId={42} revisionId={7} />, { wrapper });

    await screen.findByRole("button", { name: "Edit draft" });
    expect(screen.getAllByRole("button", { name: "Edit draft" })).toHaveLength(1);
    expect(screen.getByText("View retrieval audit")).toBeInTheDocument();
  });

  it("resolves a claim source to its real title even when it lives past the first evidence page", async () => {
    const page1Items = Array.from({ length: 100 }, (_, i) => makeEvidence({ id: i + 1, title: `Doc ${i + 1}` }));
    const page2Items = [
      makeEvidence({ id: 150, title: "Late-page Source", source_kind: "wiki", authority: "primary" }),
    ];
    listDraftEvidenceMock.mockImplementation(
      (_draftId: number, params: { job_id?: number; page?: number; per_page?: number } = {}) => {
        expect(params.job_id).toBe(5); // scoped to the resolved job, not the whole draft.
        const page = params.page ?? 1;
        if (page === 1) {
          return Promise.resolve({ items: page1Items, total: 101, page: 1, per_page: 100 });
        }
        return Promise.resolve({ items: page2Items, total: 101, page: 2, per_page: 100 });
      }
    );
    listDraftClaimsMock.mockResolvedValue(
      paginated([
        makeClaim({
          id: 1,
          sources: [
            {
              id: 1,
              claim_id: 1,
              evidence_id: 150,
              relationship: "supports",
              exact_quote: "late page quote",
              passage_start: 0,
              passage_end: 10,
              lexical_overlap_score: 0.5,
            },
          ],
        }),
      ])
    );

    render(<DraftClaimsPanel draftId={42} revisionId={7} />, { wrapper });

    expect(await screen.findByText("Late-page Source")).toBeInTheDocument();
    expect(screen.queryByText("Evidence #150")).not.toBeInTheDocument();
    expect(screen.queryByText(/some source details could not be resolved/i)).not.toBeInTheDocument();
  });

  it("shows a visible note (not silent Unknowns) when the evidence lookup hits its page bound", async () => {
    listDraftEvidenceMock.mockImplementation(
      (_draftId: number, params: { page?: number; per_page?: number } = {}) => {
        const page = params.page ?? 1;
        const items = Array.from({ length: 100 }, (_, i) => makeEvidence({ id: (page - 1) * 100 + i + 1 }));
        return Promise.resolve({ items, total: 1000, page, per_page: 100 });
      }
    );
    listDraftClaimsMock.mockResolvedValue(
      paginated([
        makeClaim({
          id: 1,
          sources: [
            {
              id: 1,
              claim_id: 1,
              evidence_id: 999, // beyond the 5-page (500-row) bound.
              relationship: "supports",
              exact_quote: "unresolvable quote",
              passage_start: 0,
              passage_end: 10,
              lexical_overlap_score: 0.3,
            },
          ],
        }),
      ])
    );

    render(<DraftClaimsPanel draftId={42} revisionId={7} />, { wrapper });

    expect(await screen.findByText(/some source details could not be resolved/i)).toBeInTheDocument();
    // The fallback is the honest exception here, not a silently mislabeled "Unknown".
    expect(screen.getByText("Evidence #999")).toBeInTheDocument();
  });
});
