/**
 * Issue #515 acceptance checks — KMS list/detail behavior.
 *
 * AC36 — KMS list race: a slow earlier search response resolving LAST must
 *        never overwrite a newer one (fetchEntries commits unconditionally).
 * AC37 — KMS load more: a pagination control must exist past the fixed
 *        per_page=200 window and fetch page 2.
 * AC38 — a11y names: KMSDetailPage controls must be reachable by accessible
 *        name (delete, cancel, title, content/body).
 * AC42 — markdown render: the read view must render entry.body markdown, not
 *        dump it raw into a <pre>.
 *
 * All tests are DISCRIMINATING: they fail on the current tree.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import React from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// ---------------------------------------------------------------------------
// Mock API module — declared before component imports (hoisted).
// ---------------------------------------------------------------------------
vi.mock("@/lib/api", () => ({
  listKMSEntries: vi.fn(),
  getKMSEntry: vi.fn(),
  createKMSEntry: vi.fn(),
  updateKMSEntry: vi.fn(),
  deleteKMSEntry: vi.fn(),
  searchKMS: vi.fn(),
  compileDocumentKMS: vi.fn(),
  recompileVaultKMS: vi.fn().mockResolvedValue({ job_id: 1, status: "pending" }),
  listKMSJobs: vi.fn().mockResolvedValue({ jobs: [] }),
  downloadDocument: vi.fn(),
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

import KMSPage from "@/pages/KMSPage";
import KMSDetailPage from "@/pages/KMSDetailPage";
import { listKMSEntries, getKMSEntry } from "@/lib/api";

const listMock = vi.mocked(listKMSEntries);
const getMock = vi.mocked(getKMSEntry);

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
});

function makeEntry(id: number, title: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    vault_id: 1,
    file_id: null,
    slug: `entry-${id}`,
    title,
    body: `Body of ${title}`,
    summary: "",
    tags_json: "[]",
    tags: [],
    source_type: "manual",
    status: "draft",
    created_by: null,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    last_compiled_at: null,
    ...overrides,
  };
}

function listResult(entries: Array<Record<string, unknown>>, page = 1, total = entries.length) {
  return { entries, total, page, per_page: 200 };
}

// ---------------------------------------------------------------------------
// AC36 — KMS list race
// ---------------------------------------------------------------------------
describe("issue515 ac36", () => {
  it("issue515-ac36 stale search responses never clobber newer KMS results", async () => {
    vi.useFakeTimers();

    const calls: Array<{ params: any; resolve: (v: any) => void }> = [];
    listMock.mockImplementation(
      (params: any) =>
        new Promise((resolve) => {
          calls.push({ params, resolve });
        })
    );

    render(
      <MemoryRouter>
        <KMSPage />
      </MemoryRouter>
    );

    // Mount fetch (empty search -> 0ms debounce).
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    await act(async () => {
      calls[0].resolve(listResult([makeEntry(300, "KMS Initial Entry")]));
    });
    expect(screen.getByText("KMS Initial Entry")).toBeInTheDocument();

    // Search A ("alpha") — slow response.
    const input = screen.getByPlaceholderText("Search title and content…");
    fireEvent.change(input, { target: { value: "alpha" } });
    await act(async () => {
      vi.advanceTimersByTime(300);
    });
    // Search B ("beta") — fast response, fired while A is still pending.
    fireEvent.change(input, { target: { value: "beta" } });
    await act(async () => {
      vi.advanceTimersByTime(300);
    });
    expect(calls.length).toBe(3); // mount + A + B
    expect(calls[1].params).toMatchObject({ search: "alpha" });
    expect(calls[2].params).toMatchObject({ search: "beta" });

    // B resolves first and must be displayed...
    await act(async () => {
      calls[2].resolve(listResult([makeEntry(2, "Beta Fast Entry")]));
    });
    expect(screen.getByText("Beta Fast Entry")).toBeInTheDocument();

    // ...and A resolving LAST must NOT replace it.
    await act(async () => {
      calls[1].resolve(listResult([makeEntry(1, "Alpha Slow Entry")]));
    });
    expect(screen.queryByText("Alpha Slow Entry")).toBeNull();
    expect(screen.getByText("Beta Fast Entry")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC37 — KMS load more
// ---------------------------------------------------------------------------
describe("issue515 ac37", () => {
  it("issue515-ac37 load more fetches page 2 and reveals the 201st entry", async () => {
    const firstBatch = Array.from({ length: 200 }, (_, i) =>
      makeEntry(i + 1, `KMS Entry ${i + 1}`)
    );
    listMock.mockImplementation(async (params: { page?: number }) => {
      if (params?.page === 2) {
        return listResult([makeEntry(201, "KMS Entry 201")], 2, 201);
      }
      return listResult(firstBatch, 1, 201);
    });

    render(
      <MemoryRouter>
        <KMSPage />
      </MemoryRouter>
    );

    expect(await screen.findByText("KMS Entry 1")).toBeInTheDocument();
    expect(screen.getByText("201 entries")).toBeInTheDocument();

    // The pagination control must exist while entries remain beyond the
    // loaded window (201 total > 200 loaded).
    const loadMore = screen.queryByRole("button", {
      name: /load more|show more|next page/i,
    });
    expect(
      loadMore,
      "a Load more / next-page control must exist when total (201) exceeds loaded entries (200)"
    ).toBeTruthy();

    fireEvent.click(loadMore!);
    await waitFor(() => {
      expect(listMock).toHaveBeenCalledWith(expect.objectContaining({ page: 2 }));
    });
    expect(await screen.findByText("KMS Entry 201")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Shared detail-page render helper (routed, so useParams resolves).
// ---------------------------------------------------------------------------
function renderKmsDetail(entry: Record<string, unknown>) {
  getMock.mockResolvedValue(entry as any);
  return render(
    <MemoryRouter initialEntries={["/kms/5"]}>
      <Routes>
        <Route path="/kms/:entryId" element={<KMSDetailPage />} />
      </Routes>
    </MemoryRouter>
  );
}

// ---------------------------------------------------------------------------
// AC38 — accessible names
// ---------------------------------------------------------------------------
describe("issue515 ac38", () => {
  it("issue515-ac38 detail controls are reachable by accessible name", async () => {
    renderKmsDetail(makeEntry(5, "Visa renewal steps"));

    expect(await screen.findByRole("heading", { name: "Visa renewal steps" })).toBeInTheDocument();

    // View mode: the delete control must be named.
    expect(screen.getByRole("button", { name: /delete/i })).toBeInTheDocument();

    // Edit mode: cancel + labeled title and content/body fields.
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /title/i })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /content|body/i })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC42 — markdown rendering in the read view
// ---------------------------------------------------------------------------
describe("issue515 ac42", () => {
  it("issue515-ac42 read view renders markdown instead of raw text", async () => {
    renderKmsDetail(
      makeEntry(5, "Markdown fidelity", { body: "# Heading\n\n**bold**" })
    );

    expect(await screen.findByRole("heading", { name: "Markdown fidelity" })).toBeInTheDocument();

    // The markdown must be parsed: a real heading element and strong text...
    expect(screen.getByRole("heading", { name: "Heading" })).toBeInTheDocument();
    const bold = screen.getByText("bold");
    expect(bold.tagName).toBe("STRONG");

    // ...with no literal markdown markers rendered.
    expect(screen.queryByText("**bold**")).toBeNull();
    expect(screen.queryByText("# Heading")).toBeNull();
  });
});
