/**
 * PR C: Wiki UI extensions for the optional LLM curator.
 *
 * Covers:
 *   - WikiPageDetail renders a "deterministic" chip for legacy /
 *     deterministic claims.
 *   - WikiPageDetail renders an "LLM curator" chip for curator-authored
 *     claims (created_by_kind === "llm_curator").
 *   - WikiPageDetail surfaces "Needs review" for status==='needs_review'
 *     so reviewers see candidates pending verification clearly.
 *   - WikiJobsPanel curator summary parses result_json and renders
 *     accepted/rejected/lint/errors counts only when present.
 *   - The summary renders nothing when curator block is missing or
 *     result_json is malformed.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
// PRR-006 (#531): WikiPageDetail renders react-router <Link> origin chips,
// so its bare mounts must sit inside a router context.
import { MemoryRouter } from "react-router-dom";
import { WikiPageDetail } from "./WikiPageDetail";
import type { WikiClaim, WikiPage, WikiCompileJob } from "@/lib/api";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

function makeClaim(overrides: Partial<WikiClaim> = {}): WikiClaim {
  return {
    id: 1,
    vault_id: 2,
    page_id: 10,
    claim_text: "Alice founded the company",
    claim_type: "fact",
    subject: "Alice",
    predicate: "founded",
    object: "the company",
    source_type: "document",
    status: "active",
    confidence: 0.9,
    created_by: null,
    created_by_kind: null,
    created_at: "2024-01-01T00:00:00",
    updated_at: "2024-01-01T00:00:00",
    sources: [],
    ...overrides,
  } as WikiClaim;
}

function makePage(claims: WikiClaim[]): WikiPage {
  return {
    id: 10,
    vault_id: 2,
    slug: "doc/alice",
    title: "Alice doc",
    page_type: "entity",
    summary: "",
    markdown: "",
    status: "draft",
    confidence: 0.0,
    created_by: null,
    created_at: "2024-01-01T00:00:00",
    updated_at: "2024-01-01T00:00:00",
    last_compiled_at: null,
    claims,
    entities: [],
    lint_findings: [],
  } as unknown as WikiPage;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("WikiPageDetail provenance chips", () => {
  it("renders 'deterministic' chip for legacy / deterministic claims", () => {
    const page = makePage([makeClaim({ created_by_kind: "deterministic" })]);
    render(
      <MemoryRouter>
        <WikiPageDetail
          page={page}
          onBack={() => {}}
          onEdit={() => {}}
          onDelete={() => {}}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText(/deterministic/i)).toBeInTheDocument();
    expect(screen.queryByText(/LLM curator/i)).not.toBeInTheDocument();
  });

  it("renders 'LLM curator' chip for curator-authored claims", () => {
    const page = makePage([makeClaim({ created_by_kind: "llm_curator" })]);
    render(
      <MemoryRouter>
        <WikiPageDetail
          page={page}
          onBack={() => {}}
          onEdit={() => {}}
          onDelete={() => {}}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText(/LLM curator/i)).toBeInTheDocument();
  });

  it("renders 'Needs review' badge when status is needs_review", () => {
    const page = makePage([
      makeClaim({
        created_by_kind: "llm_curator",
        status: "needs_review",
      }),
    ]);
    render(
      <MemoryRouter>
        <WikiPageDetail
          page={page}
          onBack={() => {}}
          onEdit={() => {}}
          onDelete={() => {}}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText(/Needs review/i)).toBeInTheDocument();
  });

  it("treats null created_by_kind as deterministic for display", () => {
    const page = makePage([makeClaim({ created_by_kind: null })]);
    render(
      <MemoryRouter>
        <WikiPageDetail
          page={page}
          onBack={() => {}}
          onEdit={() => {}}
          onDelete={() => {}}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText(/deterministic/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------
// CuratorSummary parses result_json defensively.
// ---------------------------------------------------------------------

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listWikiJobs: vi.fn(async () => ({
      jobs: ((globalThis as unknown as { __wikiJobsForTest: WikiCompileJob[] })
        .__wikiJobsForTest ?? []),
    })),
    retryWikiJob: vi.fn(async () => ({} as WikiCompileJob)),
    cancelWikiJob: vi.fn(async () => ({ job_id: 0, status: "cancelled" })),
    recompileVaultWiki: vi.fn(async () => ({ job_id: 999, status: "pending" })),
  };
});

import { WikiJobsPanel } from "./WikiJobsPanel";

function withTestJobs(jobs: WikiCompileJob[]) {
  (globalThis as unknown as { __wikiJobsForTest: WikiCompileJob[] }).__wikiJobsForTest =
    jobs;
}

function makeJob(overrides: Partial<WikiCompileJob> = {}): WikiCompileJob {
  return {
    id: 1,
    vault_id: 2,
    trigger_type: "ingest",
    trigger_id: "file:42",
    status: "completed",
    error: null,
    result_json: "{}",
    created_at: "2024-01-01T00:00:00",
    started_at: "2024-01-01T00:00:00",
    completed_at: "2024-01-01T00:00:01",
    ...overrides,
  } as WikiCompileJob;
}

describe("WikiJobsPanel curator summary", () => {
  it("renders curator counts when present in result_json", async () => {
    withTestJobs([
      makeJob({
        result_json: JSON.stringify({
          curator: {
            accepted: 3,
            rejected: 2,
            lint: 1,
            errors: ["timeout"],
            calls: 1,
          },
        }),
      }),
    ]);
    render(<WikiJobsPanel vaultId={2} />);
    expect(await screen.findByText(/Curator accepted:/i)).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    // errors=1 from the array length
    expect(screen.getByText("1", { selector: "strong.text-destructive" })).toBeInTheDocument();
  });

  it("renders nothing when curator block is missing", async () => {
    withTestJobs([makeJob({ result_json: "{}" })]);
    render(<WikiJobsPanel vaultId={2} />);
    // Wait for the panel to settle.
    expect(await screen.findByText("Compile Jobs")).toBeInTheDocument();
    expect(screen.queryByText(/Curator accepted:/i)).not.toBeInTheDocument();
  });

  it("renders nothing for malformed result_json", async () => {
    withTestJobs([makeJob({ result_json: "{not even json" })]);
    render(<WikiJobsPanel vaultId={2} />);
    expect(await screen.findByText("Compile Jobs")).toBeInTheDocument();
    expect(screen.queryByText(/Curator accepted:/i)).not.toBeInTheDocument();
  });
});
