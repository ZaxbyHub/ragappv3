/**
 * Issue #515 acceptance checks — WikiPageDetail contracts and Wiki surfaces.
 *
 * AC40 — real backend payload shapes render: version history, attachments and
 *        backlinks must render identifiable labels/dates from the ACTUAL
 *        backend payloads (wiki_store.py dataclasses), not the phantom
 *        version/edited_at/diff_summary, filename/attached_at,
 *        page_id/title/slug shapes the current interfaces expect.
 * AC43 — claims surface: WikiPage must consume listWikiClaims with a claims
 *        tab/section listing claims + statuses, and an explicit empty state.
 * AC44 — lifecycle + origin links: claim source chips must be links to the
 *        document/memory surfaces, and a lifecycle/help surface must be
 *        reachable from WikiPage.
 * AC29 — claimless clarity: 'skipped' wiki documents get a distinct label in
 *        the documents table (not a generic Compile button), and a wiki page
 *        with zero claims shows an explicit "no claims" empty state.
 *
 * All tests are DISCRIMINATING: they fail on the current tree.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import React from "react";
import { MemoryRouter } from "react-router-dom";

// ---------------------------------------------------------------------------
// Mock API module — full replacement (nothing mounted here may touch axios).
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
  resolveWikiLintFinding: vi.fn().mockResolvedValue({}),
  searchWiki: vi.fn().mockResolvedValue({ pages: [], claims: [], entities: [], query: "" }),
  promoteMemoryToWiki: vi.fn(),
  updateMemory: vi.fn(),
  bulkWikiPageAction: vi.fn(),
  getWikiPageVersions: vi.fn(),
  getWikiPageFiles: vi.fn(),
  getWikiPageBacklinks: vi.fn(),
  getWikiActivityFeed: vi.fn().mockResolvedValue([]),
  listWikiJobs: vi.fn().mockResolvedValue({ jobs: [] }),
  retryWikiJob: vi.fn(),
  cancelWikiJob: vi.fn(),
  recompileVaultWiki: vi.fn(),
  // File-name resolution paths the post-fix detail sections may use.
  getDocument: vi.fn(),
  listDocuments: vi.fn().mockResolvedValue({ documents: [] }),
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

// DocumentTable virtualizes via @tanstack/react-virtual — stub with the
// DocumentsPage.virtualization.test.tsx pattern so all rows render.
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(({ count }: { count: number }) => ({
    getVirtualItems: () =>
      Array.from({ length: count }, (_, i) => ({
        index: i,
        start: i * 56,
        size: 56,
        key: `row-${i}`,
        measureElement: vi.fn(),
      })),
    getTotalSize: () => count * 56,
    measureElement: vi.fn(),
    scrollToIndex: vi.fn(),
    measure: vi.fn(),
  })),
}));

import { WikiPageDetail } from "@/pages/WikiPageDetail";
import WikiPage from "@/pages/WikiPage";
import { DocumentTable } from "@/components/documents/DocumentTable";
import {
  getWikiPageVersions,
  getWikiPageFiles,
  getWikiPageBacklinks,
  getWikiPage,
  listWikiPages,
  listWikiClaims,
  getDocument,
} from "@/lib/api";

const versionsMock = vi.mocked(getWikiPageVersions);
const filesMock = vi.mocked(getWikiPageFiles);
const backlinksMock = vi.mocked(getWikiPageBacklinks);
const getMock = vi.mocked(getWikiPage);
const listMock = vi.mocked(listWikiPages);
const claimsMock = vi.mocked(listWikiClaims);
const getDocMock = vi.mocked(getDocument);

beforeEach(() => {
  vi.clearAllMocks();
  // WikiPage mounts the wiki-events fetch stream — keep it open forever.
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
  listMock.mockResolvedValue({ pages: [], page: 1, per_page: 50 });
  claimsMock.mockResolvedValue({ claims: [] });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function makeWikiPage(overrides: Record<string, unknown> = {}) {
  return {
    id: 10,
    vault_id: 2,
    slug: "doc/alice",
    title: "Alice doc",
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

function renderWikiPageDetail(page: ReturnType<typeof makeWikiPage>) {
  return render(
    <MemoryRouter>
      <WikiPageDetail page={page as any} onBack={() => {}} onEdit={() => {}} onDelete={() => {}} />
    </MemoryRouter>
  );
}

// ---------------------------------------------------------------------------
// AC40 — real backend payload shapes
// ---------------------------------------------------------------------------
describe("issue515 ac40", () => {
  it("issue515-ac40 real backend payload shapes render in detail sections", async () => {
    // Exact backend shapes (backend/app/services/wiki_store.py dataclasses +
    // backend/app/api/routes/wiki.py serialization):
    versionsMock.mockResolvedValue({
      versions: [
        {
          id: 301,
          page_id: 10,
          vault_id: 2,
          title: "Alice doc",
          markdown: "# older",
          summary: "",
          status: "draft",
          confidence: 0.5,
          edited_by: 7,
          created_at: "2024-03-04T05:06:07Z",
        },
      ],
    });
    filesMock.mockResolvedValue({
      files: [{ id: 401, page_id: 10, file_id: 7, vault_id: 2, created_at: "2024-01-01T00:00:00Z" }],
    });
    backlinksMock.mockResolvedValue({
      backlinks: [
        {
          id: 501,
          source_page_id: 42,
          target_page_id: 10,
          vault_id: 2,
          link_text: "see the linking page",
          created_at: "2024-01-01T00:00:00Z",
        },
      ],
    });
    // Resolution paths the post-fix sections may use for ids -> labels.
    getMock.mockImplementation(async (pageId: number) =>
      makeWikiPage({ id: pageId, title: "Linking Page", slug: "doc/linking" })
    );
    listMock.mockResolvedValue({
      pages: [makeWikiPage({ id: 42, title: "Linking Page", slug: "doc/linking" })],
      page: 1,
      per_page: 50,
    });
    getDocMock.mockResolvedValue({ id: 7, filename: "spec.pdf" } as any);

    renderWikiPageDetail(makeWikiPage());

    // --- Version history: formatted date + identifiable version ---
    fireEvent.click(screen.getByText("Version History"));
    await waitFor(() => expect(versionsMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/invalid date/i)).toBeNull();
    expect(screen.getByText(/2024/)).toBeInTheDocument();
    expect(screen.getByText(/^(v|version[ :#]*)\d+$/i)).toBeInTheDocument();

    // --- Attachments: an identifying label (filename or file id) ---
    fireEvent.click(screen.getByText("Attachments"));
    await waitFor(() => expect(filesMock).toHaveBeenCalledTimes(1));
    expect(screen.getByText(/spec\.pdf|file[ #:]*7/i)).toBeInTheDocument();

    // --- Backlinks: the source page title/slug ---
    fireEvent.click(screen.getByText("Backlinks"));
    await waitFor(() => expect(backlinksMock).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Linking Page")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC43 — claims tab/section on WikiPage
// ---------------------------------------------------------------------------
describe("issue515 ac43", () => {
  const claimA = {
    id: 1,
    vault_id: 1,
    page_id: null,
    claim_text: "Claims tab renders the first claim",
    claim_type: "fact",
    subject: "wiki",
    predicate: "renders",
    object: "claims",
    source_type: "manual",
    status: "active",
    confidence: 0.9,
    created_by: null,
    created_by_kind: "deterministic",
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    sources: [],
  };
  const claimB = {
    ...claimA,
    id: 2,
    claim_text: "Claims tab renders the second claim",
    status: "needs_review",
  };

  it("issue515-ac43 claims tab lists claims with statuses and an empty state", async () => {
    claimsMock.mockResolvedValue({ claims: [claimA, claimB] as any });

    const { unmount } = render(
      <MemoryRouter>
        <WikiPage />
      </MemoryRouter>
    );

    // A claims surface (tab or section) must exist and be reachable...
    const surface =
      screen.queryByRole("tab", { name: /claims?/i }) ??
      screen.queryByRole("heading", { name: /claims?/i });
    expect(surface).not.toBeNull();

    // ...listing every claim with a status marker.
    await waitFor(() => {
      expect(screen.getByText(/first claim/)).toBeInTheDocument();
      expect(screen.getByText(/second claim/)).toBeInTheDocument();
    });
    expect(screen.getByText(/needs[ -]?review/i)).toBeInTheDocument();

    unmount();

    // Empty result -> an explicit "no claims" empty state.
    claimsMock.mockResolvedValue({ claims: [] });
    render(
      <MemoryRouter>
        <WikiPage />
      </MemoryRouter>
    );
    await waitFor(() => expect(claimsMock).toHaveBeenCalled());
    expect(screen.getByText(/no claims/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC44 — lifecycle + origin links
// ---------------------------------------------------------------------------
describe("issue515 ac44", () => {
  it("issue515-ac44 claim sources link to origins and lifecycle help is reachable", async () => {
    // (a) Source chips must be LINKS to the document/memory surfaces.
    const claim = {
      id: 1,
      vault_id: 1,
      page_id: 10,
      claim_text: "A claim with origin chips",
      claim_type: "fact",
      subject: null,
      predicate: null,
      object: null,
      source_type: "mixed",
      status: "active",
      confidence: 0.8,
      created_by: null,
      created_by_kind: "deterministic",
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
      sources: [
        {
          id: 11,
          claim_id: 1,
          source_kind: "document",
          file_id: 7,
          chunk_id: null,
          memory_id: null,
          chat_message_id: null,
          source_label: "spec.pdf",
          quote: null,
          char_start: null,
          char_end: null,
          page_number: 1,
          confidence: 0.9,
          created_at: "2024-01-01T00:00:00Z",
        },
        {
          id: 12,
          claim_id: 1,
          source_kind: "memory",
          file_id: null,
          chunk_id: null,
          memory_id: 3,
          chat_message_id: null,
          source_label: "chat memory",
          quote: null,
          char_start: null,
          char_end: null,
          page_number: null,
          confidence: 0.9,
          created_at: "2024-01-01T00:00:00Z",
        },
      ],
    };
    const { unmount } = renderWikiPageDetail(makeWikiPage({ claims: [claim] as any }));

    const links = screen.getAllByRole("link");
    const docLink = links.find((l) => (l.getAttribute("href") ?? "").includes("/documents/7"));
    expect(docLink, "document source chip must link to /documents/7").toBeTruthy();
    expect((docLink!.textContent ?? "").toLowerCase()).toContain("document");
    const memLink = links.find((l) => (l.getAttribute("href") ?? "").includes("/memory"));
    expect(memLink, "memory source chip must link to the /memory surface").toBeTruthy();
    expect((memLink!.textContent ?? "").toLowerCase()).toContain("memory");

    unmount();

    // (b) A lifecycle/help surface explaining the knowledge surfaces is
    //     reachable from WikiPage.
    render(
      <MemoryRouter>
        <WikiPage />
      </MemoryRouter>
    );
    const helpSurface =
      screen.queryByRole("heading", { name: /how .* works|lifecycle/i }) ??
      screen.queryByRole("button", { name: /how .* works|lifecycle/i }) ??
      screen.queryByText(/how (knowledge|documents|memor(y|ies)|wiki|kms) work|lifecycle/i);
    expect(helpSurface).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// AC29 — claimless page clarity
// ---------------------------------------------------------------------------
describe("issue515 ac29", () => {
  it("issue515-ac29 skipped docs and claimless pages get distinct labels", () => {
    // (a) DocumentTable: wiki_status 'skipped' must be distinguishable from
    //     'not_compiled' (which keeps the Compile button).
    const docs = [
      { id: "1", filename: "fresh-notes.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 3 } },
      { id: "2", filename: "scan-only.jpg.pdf", size: 2048, created_at: "2024-01-02", metadata: { status: "processed", chunk_count: 0 } },
    ];
    const wikiStatus = (fileId: number, status: string) => ({
      file_id: fileId,
      wiki_status: status,
      pages_count: 0,
      claims_count: 0,
      active_claims: 0,
      lint_count: 0,
      pages: [],
      latest_job: null,
      job_count: 0,
    });
    const noop = vi.fn();
    render(
      <MemoryRouter>
        <DocumentTable
          documents={docs as any}
          selectedIds={new Set<string>()}
          canMutateDocuments={true}
          filenameColWidth={220}
          onResizeMouseDown={noop}
          onResizeKeyDown={noop}
          onResizeTouchStart={noop}
          onSelectAll={noop}
          onSelectOne={noop}
          wikiStatusMap={{
            "1": wikiStatus(1, "not_compiled") as any,
            "2": wikiStatus(2, "skipped") as any,
          }}
          compilingDocIds={new Set<string>()}
          onCompileDocument={noop}
          onDownload={noop}
          onDelete={noop}
          sortBy={"filename" as any}
          sortOrder={"desc" as any}
          onSort={noop}
        />
      </MemoryRouter>
    );

    const notCompiledRow = screen.getByText("fresh-notes.pdf").closest("tr")!;
    const skippedRow = screen.getByText("scan-only.jpg.pdf").closest("tr")!;
    expect(notCompiledRow).toBeTruthy();
    expect(skippedRow).toBeTruthy();

    // not_compiled keeps the generic Compile action...
    expect(within(notCompiledRow).getByRole("button", { name: /compile/i })).toBeInTheDocument();
    // ...while skipped shows a distinct label and NO compile affordance.
    expect(within(skippedRow).queryByRole("button", { name: /compile/i })).toBeNull();
    expect(within(skippedRow).getByText(/skipped|no extractable knowledge/i)).toBeInTheDocument();

    // (b) WikiPageDetail with zero claims -> explicit empty state.
    renderWikiPageDetail(makeWikiPage({ claims: [] }));
    expect(screen.getByText(/no claims/i)).toBeInTheDocument();
  });
});
