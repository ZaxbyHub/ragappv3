/**
 * Issue #515 acceptance checks — Wiki list pagination and lint dismissal.
 *
 * AC34 — pagination: WikiPage/WikiPageList must expose a load-more control
 *        wired to listWikiPages page/per_page, retaining active filters on the
 *        follow-up request.
 * AC35 — lint dismissal sticks: after dismissing a finding, the panel refresh
 *        must NOT re-list the same finding as open (the current refresh
 *        re-runs lint and the panel blindly commits whatever comes back,
 *        including a recreated open copy of the just-dismissed finding).
 *
 * All tests are DISCRIMINATING: they fail on the current tree.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import React from "react";

// ---------------------------------------------------------------------------
// Mock API module — declared before component imports (hoisted).
// ---------------------------------------------------------------------------
vi.mock("@/lib/api", () => ({
  listWikiPages: vi.fn(),
  getWikiPage: vi.fn(),
  createWikiPage: vi.fn(),
  updateWikiPage: vi.fn(),
  deleteWikiPage: vi.fn(),
  listWikiEntities: vi.fn().mockResolvedValue({ entities: [] }),
  listWikiClaims: vi.fn().mockResolvedValue({ claims: [] }),
  listWikiLintFindings: vi.fn().mockResolvedValue({ findings: [] }),
  runWikiLint: vi.fn().mockResolvedValue({ findings: [], count: 0 }),
  resolveWikiLintFinding: vi.fn(),
  searchWiki: vi.fn().mockResolvedValue({ pages: [], claims: [], entities: [], query: "" }),
  promoteMemoryToWiki: vi.fn(),
  updateMemory: vi.fn(),
  bulkWikiPageAction: vi.fn(),
  getWikiPageVersions: vi.fn().mockResolvedValue({ versions: [] }),
  getWikiPageFiles: vi.fn().mockResolvedValue({ files: [] }),
  getWikiPageBacklinks: vi.fn().mockResolvedValue({ backlinks: [] }),
  getWikiActivityFeed: vi.fn().mockResolvedValue([]),
  listWikiJobs: vi.fn().mockResolvedValue({ jobs: [] }),
  retryWikiJob: vi.fn(),
  cancelWikiJob: vi.fn(),
  recompileVaultWiki: vi.fn(),
  // useWikiEventStream reads these from the barrel.
  API_BASE_URL: "/api",
  getJwtAccessToken: vi.fn(() => null),
  refreshAccessToken: vi.fn(),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: () => ({ activeVaultId: 1 }),
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector">VaultSelector</div>,
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

import WikiPage from "@/pages/WikiPage";
import { listWikiPages, listWikiLintFindings, runWikiLint, resolveWikiLintFinding } from "@/lib/api";

const listMock = vi.mocked(listWikiPages);
const lintListMock = vi.mocked(listWikiLintFindings);
const runLintApiMock = vi.mocked(runWikiLint);
const resolveMock = vi.mocked(resolveWikiLintFinding);

beforeEach(() => {
  vi.clearAllMocks();
  // WikiPage opens the wiki-events fetch stream on mount — keep it open.
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            read: () => new Promise<{ value?: Uint8Array; done: boolean }>(() => {}),
            cancel: vi.fn(),
          }),
        },
      } as unknown as Response)
    )
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function makeWikiPage(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    vault_id: 1,
    slug: "page-slug",
    title: "Page",
    page_type: "entity",
    markdown: "",
    summary: "",
    status: "draft",
    confidence: 0,
    version: 1,
    created_by: null,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    last_compiled_at: null,
    claims: [],
    entities: [],
    lint_findings: [],
    ...overrides,
  };
}

const flush = () => act(async () => {});

// ---------------------------------------------------------------------------
// AC34 — pagination / load more
// ---------------------------------------------------------------------------
describe("issue515 ac34", () => {
  it("issue515-ac34 load more fetches page 2 with filters retained", async () => {
    const pageOne = Array.from({ length: 50 }, (_, i) =>
      makeWikiPage({ id: i + 1, title: `Release Note Page ${i + 1}`, slug: `release-${i + 1}` })
    );
    const pageTwo = [makeWikiPage({ id: 51, title: "Release Note Page 51", slug: "release-51" })];

    listMock.mockImplementation(async (params: { page?: number }) => {
      if (params?.page === 2) {
        return { pages: pageTwo, page: 2, per_page: 50, total: 51 };
      }
      return { pages: pageOne, page: 1, per_page: 50, total: 51 };
    });

    render(<WikiPage />);
    expect(await screen.findByText("Release Note Page 1")).toBeInTheDocument();

    // Establish an active search so filter retention on page 2 is provable.
    const searchInput = screen.getByPlaceholderText("Search wiki...");
    fireEvent.change(searchInput, { target: { value: "release" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(listMock).toHaveBeenCalledWith(
        expect.objectContaining({ vault_id: 1, search: "release" })
      );
    });

    // The pagination control must exist while more pages remain (total 51 > 50).
    const loadMore = screen.queryByRole("button", {
      name: /load more|show more|next page/i,
    });
    expect(
      loadMore,
      "a Load more / next-page control must exist when total (51) exceeds loaded pages (50)"
    ).toBeTruthy();

    fireEvent.click(loadMore!);
    await waitFor(() => {
      expect(listMock).toHaveBeenCalledWith(
        expect.objectContaining({ page: 2, search: "release" })
      );
    });
    expect(await screen.findByText("Release Note Page 51")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC35 — lint dismissal sticks
// ---------------------------------------------------------------------------
describe("issue515 ac35", () => {
  it("issue515-ac35 dismissed finding is not re-listed as open after refresh", async () => {
    const FINDING_TITLE = "Orphaned reference detected";

    // State machine simulating the backend contract:
    //  - initial open list contains finding X (#1).
    //  - once X has been dismissed, re-LISTING returns nothing open (the
    //    dismissed finding no longer appears as open).
    //  - re-RUNNING lint still recreates an OPEN copy of X (#999) — exactly
    //    what the current backend does (clear-open-then-recreate, no
    //    fingerprint). The frontend must not let that resurrect the finding
    //    the user just dismissed.
    let dismissed = false;
    lintListMock.mockImplementation(async () => ({
      findings: dismissed
        ? []
        : [
            {
              id: 1,
              vault_id: 1,
              finding_type: "orphan",
              severity: "medium",
              title: FINDING_TITLE,
              details: "Page links nowhere.",
              related_page_ids_json: "[]",
              related_claim_ids_json: "[]",
              status: "open",
              created_at: "2024-01-01T00:00:00Z",
              updated_at: "2024-01-01T00:00:00Z",
            },
          ],
    }));
    runLintApiMock.mockImplementation(async () => ({
      findings: [
        {
          id: 999, // recreated row with a NEW id, still open
          vault_id: 1,
          finding_type: "orphan",
          severity: "medium",
          title: FINDING_TITLE,
          details: "Page links nowhere.",
          related_page_ids_json: "[]",
          related_claim_ids_json: "[]",
          status: "open",
          created_at: "2024-01-01T00:00:00Z",
          updated_at: "2024-01-01T00:00:00Z",
        },
      ],
      count: 1,
    }));

    let resolveDismiss!: (value: unknown) => void;
    resolveMock.mockImplementation(async () => {
      dismissed = true;
      return new Promise((res) => {
        resolveDismiss = res;
      }).then(() => ({}));
    });

    render(<WikiPage />);

    // Open the lint panel (header toggle — exact name so "Run Lint" inside
    // the panel can never collide).
    const lintToggle = await screen.findByRole("button", { name: /^lint\b(\s*\(\d+\))?$/i });
    fireEvent.click(lintToggle);
    expect(await screen.findByText(FINDING_TITLE)).toBeInTheDocument();

    // Dismiss the finding.
    fireEvent.click(screen.getByTitle("Dismiss"));
    await waitFor(() => expect(resolveMock).toHaveBeenCalled());
    await act(async () => {
      resolveDismiss(undefined);
    });
    // Let the refresh chain (resolve -> panel refresh / re-lint) settle.
    for (let i = 0; i < 5; i++) {
      await flush();
    }

    // Whatever refresh the panel performed, the dismissed finding must not
    // come back as an open item.
    expect(screen.queryByText(FINDING_TITLE)).toBeNull();
  });
});
