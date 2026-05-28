/**
 * WikiPageDetail header callbacks + lazy-fetch collapsible sections.
 *
 * Covers (distinct from WikiPageDetail.curator.test.tsx, which owns the
 * ClaimRow provenance / needs-review chips):
 *   - Header Back/Edit/Delete buttons invoke onBack/onEdit/onDelete.
 *   - VersionHistorySection lazy-fetches getWikiPageVersions on expand only,
 *     tolerating both array and {versions:[...]} shapes.
 *   - AttachmentsSection lazy-fetches getWikiPageFiles; empty -> empty state.
 *   - BacklinksSection lazy-fetches getWikiPageBacklinks and renders entries.
 *   - A rejected fetch is swallowed to [] and shows the empty state.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render as rtlRender,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { WikiPage } from "@/lib/api";
import {
  getWikiPageVersions,
  getWikiPageFiles,
  getWikiPageBacklinks,
} from "@/lib/api";
import { WikiPageDetail } from "./WikiPageDetail";

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

// Partial-spread so unrelated api exports (types, other fns) keep working.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getWikiPageVersions: vi.fn(),
    getWikiPageFiles: vi.fn(),
    getWikiPageBacklinks: vi.fn(),
  };
});

const mockGetVersions = vi.mocked(getWikiPageVersions);
const mockGetFiles = vi.mocked(getWikiPageFiles);
const mockGetBacklinks = vi.mocked(getWikiPageBacklinks);

function makePage(overrides: Partial<WikiPage> = {}): WikiPage {
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
    claims: [],
    entities: [],
    lint_findings: [],
    ...overrides,
  } as unknown as WikiPage;
}

function renderDetail(
  cbs: Partial<{
    onBack: () => void;
    onEdit: () => void;
    onDelete: () => void;
  }> = {},
) {
  const page = makePage();
  return render(
    <WikiPageDetail
      page={page}
      onBack={cbs.onBack ?? (() => {})}
      onEdit={cbs.onEdit ?? (() => {})}
      onDelete={cbs.onDelete ?? (() => {})}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // Default each fetcher to a resolved empty list so sections never throw
  // unless a test explicitly overrides the resolution.
  mockGetVersions.mockResolvedValue([]);
  mockGetFiles.mockResolvedValue([]);
  mockGetBacklinks.mockResolvedValue([]);
});

describe("WikiPageDetail header callbacks", () => {
  it("calls onBack when the Back button is clicked", () => {
    const onBack = vi.fn();
    renderDetail({ onBack });
    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("calls onEdit when the Edit button is clicked", () => {
    const onEdit = vi.fn();
    renderDetail({ onEdit });
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));
    expect(onEdit).toHaveBeenCalledTimes(1);
  });

  it("calls onDelete when the Delete button is clicked", () => {
    const onDelete = vi.fn();
    // The delete button has only a Trash2 icon (no text label); it is the
    // sole outline button that is neither Back nor Edit. Grab it by position.
    renderDetail({ onDelete });
    const buttons = screen.getAllByRole("button");
    // Back, Edit, Delete + three collapsible section headers (not buttons).
    const deleteBtn = buttons.find((b) =>
      b.className.includes("text-destructive"),
    );
    expect(deleteBtn).toBeTruthy();
    fireEvent.click(deleteBtn!);
    expect(onDelete).toHaveBeenCalledTimes(1);
  });
});

describe("WikiPageDetail VersionHistorySection", () => {
  it("does not fetch until expanded, then fetches once (array shape)", async () => {
    mockGetVersions.mockResolvedValue([
      {
        version: 1,
        edited_by: "alice",
        edited_at: "2024-01-01T00:00:00",
        diff_summary: "initial revision",
      },
    ]);
    renderDetail();

    // Collapsed: fetch not called.
    expect(mockGetVersions).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("Version History"));

    await waitFor(() => expect(mockGetVersions).toHaveBeenCalledTimes(1));
    expect(mockGetVersions).toHaveBeenCalledWith(10, 2);
    expect(await screen.findByText("v1")).toBeInTheDocument();
    expect(screen.getByText("initial revision")).toBeInTheDocument();
  });

  it("tolerates the {versions:[...]} object shape", async () => {
    mockGetVersions.mockResolvedValue({
      versions: [
        {
          version: 7,
          edited_by: null,
          edited_at: "2024-02-02T00:00:00",
          diff_summary: null,
        },
      ],
    });
    renderDetail();
    fireEvent.click(screen.getByText("Version History"));
    expect(await screen.findByText("v7")).toBeInTheDocument();
  });
});

describe("WikiPageDetail AttachmentsSection", () => {
  it("fetches files on expand and renders them", async () => {
    mockGetFiles.mockResolvedValue([
      { file_id: 5, filename: "spec.pdf", attached_at: "2024-01-01T00:00:00" },
    ]);
    renderDetail();
    expect(mockGetFiles).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("Attachments"));

    await waitFor(() => expect(mockGetFiles).toHaveBeenCalledTimes(1));
    expect(mockGetFiles).toHaveBeenCalledWith(10, 2);
    expect(await screen.findByText("spec.pdf")).toBeInTheDocument();
  });

  it("shows the empty state when no files are returned", async () => {
    mockGetFiles.mockResolvedValue([]);
    renderDetail();
    fireEvent.click(screen.getByText("Attachments"));
    expect(await screen.findByText("No attachments.")).toBeInTheDocument();
  });
});

describe("WikiPageDetail BacklinksSection", () => {
  it("fetches backlinks on expand and renders them (array shape)", async () => {
    mockGetBacklinks.mockResolvedValue([
      { page_id: 42, title: "Linking Page", slug: "doc/linking" },
    ]);
    renderDetail();
    expect(mockGetBacklinks).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("Backlinks"));

    await waitFor(() => expect(mockGetBacklinks).toHaveBeenCalledTimes(1));
    expect(mockGetBacklinks).toHaveBeenCalledWith(10, 2);
    expect(await screen.findByText("Linking Page")).toBeInTheDocument();
    expect(screen.getByText("doc/linking")).toBeInTheDocument();
  });

  it("tolerates the {backlinks:[...]} object shape", async () => {
    mockGetBacklinks.mockResolvedValue({
      backlinks: [{ page_id: 99, title: "Wrapped Link", slug: "doc/wrapped" }],
    });
    renderDetail();
    fireEvent.click(screen.getByText("Backlinks"));
    expect(await screen.findByText("Wrapped Link")).toBeInTheDocument();
  });
});

describe("WikiPageDetail fetch error handling", () => {
  it("swallows a rejected versions fetch and shows the empty state", async () => {
    mockGetVersions.mockRejectedValue(new Error("network down"));
    renderDetail();
    fireEvent.click(screen.getByText("Version History"));

    await waitFor(() => expect(mockGetVersions).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("No version history available."),
    ).toBeInTheDocument();
  });

  it("swallows a rejected backlinks fetch and shows the empty state", async () => {
    mockGetBacklinks.mockRejectedValue(new Error("boom"));
    renderDetail();
    fireEvent.click(screen.getByText("Backlinks"));

    await waitFor(() => expect(mockGetBacklinks).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("No pages link to this page."),
    ).toBeInTheDocument();
  });
});
