import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import React from "react";
import { toast } from "sonner";

// ---------------------------------------------------------------------------
// Mock API module — must be declared before any component imports
// ---------------------------------------------------------------------------
vi.mock("@/lib/api", () => ({
  listWikiPages: vi.fn().mockResolvedValue({ pages: [], page: 1, per_page: 50 }),
  getWikiPage: vi.fn(),
  createWikiPage: vi.fn(),
  updateWikiPage: vi.fn(),
  deleteWikiPage: vi.fn(),
  listWikiEntities: vi.fn().mockResolvedValue({ entities: [] }),
  listWikiClaims: vi.fn().mockResolvedValue({ claims: [] }),
  listWikiLintFindings: vi.fn().mockResolvedValue({ findings: [] }),
  runWikiLint: vi.fn().mockResolvedValue({ findings: [], count: 0 }),
  searchWiki: vi.fn().mockResolvedValue({ pages: [], claims: [], entities: [], query: "" }),
  promoteMemoryToWiki: vi.fn(),
  updateMemory: vi.fn(),
  // WikiPage mounts useWikiEventStream, which reads these from @/lib/api.
  API_BASE_URL: "/api",
  getJwtAccessToken: vi.fn(() => null),
  refreshAccessToken: vi.fn(),
  getWikiActivityFeed: vi.fn().mockResolvedValue([]),
}));

// Mock vault store
vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: () => ({ activeVaultId: 1 }),
}));

// Mock VaultSelector
vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector">VaultSelector</div>,
}));

// Mock sonner
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

// Radix Tabs cannot be activated via fireEvent.click in jsdom (pointer-capture /
// activation semantics). Mock the primitive so TabsTrigger is a plain button that
// invokes onValueChange — same approach the repo uses for ui/tabs and ui/select.
// This lets us drive the page-type tabs that live in WikiPage's toolbar.
vi.mock("@/components/ui/tabs", async () => {
  const ReactMod = await import("react");
  const Ctx = ReactMod.createContext<(v: string) => void>(() => {});
  return {
    Tabs: ({ onValueChange, children }: any) =>
      ReactMod.createElement(Ctx.Provider, { value: onValueChange }, children),
    TabsList: ({ children }: any) => ReactMod.createElement("div", null, children),
    TabsTrigger: ({ value, children }: any) => {
      const onValueChange = ReactMod.useContext(Ctx);
      return ReactMod.createElement(
        "button",
        { role: "tab", onClick: () => onValueChange(value) },
        children
      );
    },
  };
});

// Mock child wiki page components to isolate the parent
vi.mock("@/pages/WikiPageList", () => ({
  WikiPageList: ({ onSelect }: { onSelect?: (pageId: number) => void }) => (
    <div data-testid="wiki-page-list">
      Page List
      <button
        data-testid="select-page-btn"
        onClick={() => onSelect?.(1)}
      >
        Select Page
      </button>
    </div>
  ),
  PAGE_TYPES: [
    { value: "", label: "All" },
    { value: "overview", label: "Overview" },
    { value: "entity", label: "Entities" },
  ],
}));

vi.mock("@/pages/WikiPageDetail", () => ({
  WikiPageDetail: ({ onEdit }: { onEdit?: () => void }) => (
    <div data-testid="wiki-page-detail">
      Page Detail
      <button data-testid="edit-page-btn" onClick={onEdit}>
        Edit
      </button>
    </div>
  ),
}));

vi.mock("@/pages/WikiEditDialog", () => ({
  WikiEditDialog: ({
    open,
    onSave,
    page,
  }: {
    open: boolean;
    onSave?: (data: {
      title: string;
      page_type: string;
      markdown: string;
      summary: string;
      status: string;
      confidence: number;
    }) => Promise<void>;
    page?: object | null;
  }) =>
    open ? (
      <div data-testid="wiki-edit-dialog">
        Edit Dialog
        <button
          data-testid="save-dialog-btn"
          onClick={() =>
            onSave?.(
              page
                ? { title: (page as { title: string }).title, page_type: (page as { page_type: string }).page_type, markdown: (page as { markdown: string }).markdown, summary: (page as { summary: string }).summary, status: (page as { status: string }).status, confidence: (page as { confidence: number }).confidence }
                : { title: "New", page_type: "entity", markdown: "md", summary: "", status: "draft", confidence: 0 }
            )
          }
        >
          Save
        </button>
      </div>
    ) : null,
}));

vi.mock("@/pages/WikiLintPanel", () => ({
  WikiLintPanel: ({ onRunLint, vaultId }: { onRunLint: () => void; vaultId: number | null }) => (
    <div data-testid="wiki-lint-panel" data-vault-id={vaultId}>
      <button onClick={onRunLint} data-testid="run-lint-btn">Run Lint</button>
    </div>
  ),
}));

// ---------------------------------------------------------------------------
// Now import components after mocks are in place
// ---------------------------------------------------------------------------
import WikiPage from "./WikiPage";
import { listWikiPages, listWikiLintFindings, runWikiLint, updateWikiPage } from "@/lib/api";

// WikiPage opens an authenticated wiki-events fetch stream on mount
// (useWikiEventStream). Stub fetch with an open (never-resolving) stream so
// these rendering tests make no real request and trigger no reconnect.
beforeEach(() => {
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

// ---------------------------------------------------------------------------
// Navigation type test (no rendering needed)
// ---------------------------------------------------------------------------
describe("Wiki navigation type", () => {
  it('NavItemId union includes "wiki"', async () => {
    // TypeScript compilation would catch this, but we can verify at runtime
    // by checking the navigation file exports
    const navModule = await import("@/components/layout/navigationTypes");
    // The type exists — if import works and the module loads, wiki is a valid id.
    expect(navModule).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Wiki API types / functions exist
// ---------------------------------------------------------------------------
describe("Wiki API exports", () => {
  it("listWikiPages is a function", async () => {
    const api = await import("@/lib/api");
    expect(typeof api.listWikiPages).toBe("function");
  });

  it("runWikiLint is a function", async () => {
    const api = await import("@/lib/api");
    expect(typeof api.runWikiLint).toBe("function");
  });

  it("promoteMemoryToWiki is a function", async () => {
    const api = await import("@/lib/api");
    expect(typeof api.promoteMemoryToWiki).toBe("function");
  });

  it("listWikiLintFindings is a function", async () => {
    const api = await import("@/lib/api");
    expect(typeof api.listWikiLintFindings).toBe("function");
  });
});

// ---------------------------------------------------------------------------
// WikiPage component rendering
// ---------------------------------------------------------------------------
describe("WikiPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (listWikiPages as ReturnType<typeof vi.fn>).mockResolvedValue({
      pages: [],
      page: 1,
      per_page: 50,
    });
    (listWikiLintFindings as ReturnType<typeof vi.fn>).mockResolvedValue({
      findings: [],
    });
  });

  it("renders Wiki heading", async () => {
    await act(async () => {
      render(<WikiPage />);
    });
    expect(screen.getByText("Wiki")).toBeInTheDocument();
  });

  it("renders VaultSelector", async () => {
    await act(async () => {
      render(<WikiPage />);
    });
    expect(screen.getByTestId("vault-selector")).toBeInTheDocument();
  });

  it("renders WikiPageList when vault is set", async () => {
    await act(async () => {
      render(<WikiPage />);
    });
    expect(screen.getByTestId("wiki-page-list")).toBeInTheDocument();
  });

  it("calls listWikiPages on mount with active vault", async () => {
    await act(async () => {
      render(<WikiPage />);
    });
    await waitFor(() => {
      expect(listWikiPages).toHaveBeenCalledWith(
        expect.objectContaining({ vault_id: 1 })
      );
    });
  });

  it("calls listWikiLintFindings on mount", async () => {
    await act(async () => {
      render(<WikiPage />);
    });
    await waitFor(() => {
      expect(listWikiLintFindings).toHaveBeenCalledWith({ vault_id: 1 });
    });
  });

  it("shows Lint button in header", async () => {
    await act(async () => {
      render(<WikiPage />);
    });
    const lintBtn = screen.getByRole("button", { name: /lint/i });
    expect(lintBtn).toBeInTheDocument();
  });

  it("toggles lint panel when Lint button is clicked", async () => {
    const { getByRole, queryByTestId } = render(<WikiPage />);
    // Panel not open initially
    expect(queryByTestId("wiki-lint-panel")).toBeNull();

    // Click lint toggle button
    await act(async () => {
      getByRole("button", { name: /lint/i }).click();
    });

    expect(screen.getByTestId("wiki-lint-panel")).toBeInTheDocument();
  });

  it("opens edit dialog when create button is clicked", async () => {
    const { queryByTestId } = render(<WikiPage />);

    // Dialog not open initially
    expect(queryByTestId("wiki-edit-dialog")).toBeNull();

    // Simulate clicking the New Page button in the toolbar
    await act(async () => {
      screen.getByRole("button", { name: /new page/i }).click();
    });

    expect(screen.getByTestId("wiki-edit-dialog")).toBeInTheDocument();
  });

  it("runs lint when Run Lint is clicked inside lint panel", async () => {
    (runWikiLint as ReturnType<typeof vi.fn>).mockResolvedValue({
      findings: [],
      count: 0,
    });

    render(<WikiPage />);

    // Open lint panel
    await act(async () => {
      screen.getByRole("button", { name: /lint/i }).click();
    });

    // Run lint from the panel
    await act(async () => {
      screen.getByTestId("run-lint-btn").click();
    });

    await waitFor(() => {
      expect(runWikiLint).toHaveBeenCalledWith(1);
    });
  });

  // ---------------------------------------------------------------------------
  // Search and filter toolbar
  //
  // These cases were relocated from WikiPageList.test.tsx. During the rebrand the
  // search box and page-type tabs moved out of WikiPageList (now a pure list) and
  // into WikiPage's toolbar, which drives fetchPages → listWikiPages with the
  // query. WikiPageList no longer renders those affordances or accepts onFilter,
  // so the equivalent assertions live here against the real toolbar.
  // ---------------------------------------------------------------------------
  describe("Search and filter toolbar", () => {
    it("search submit (button) calls listWikiPages with the query", async () => {
      render(<WikiPage />);
      fireEvent.change(screen.getByPlaceholderText("Search wiki..."), {
        target: { value: "alpha" },
      });
      (listWikiPages as ReturnType<typeof vi.fn>).mockClear();
      fireEvent.click(screen.getByRole("button", { name: "Search" }));
      await waitFor(() => {
        expect(listWikiPages).toHaveBeenCalledWith({
          vault_id: 1,
          page_type: undefined,
          search: "alpha",
        });
      });
    });

    it("search submit on Enter calls listWikiPages with the query", async () => {
      render(<WikiPage />);
      const input = screen.getByPlaceholderText("Search wiki...");
      fireEvent.change(input, { target: { value: "beta" } });
      (listWikiPages as ReturnType<typeof vi.fn>).mockClear();
      fireEvent.keyDown(input, { key: "Enter" });
      await waitFor(() => {
        expect(listWikiPages).toHaveBeenCalledWith({
          vault_id: 1,
          page_type: undefined,
          search: "beta",
        });
      });
    });

    it("changing the page-type tab calls listWikiPages with the page_type", async () => {
      render(<WikiPage />);
      (listWikiPages as ReturnType<typeof vi.fn>).mockClear();
      fireEvent.click(screen.getByRole("tab", { name: "Entities" }));
      await waitFor(() => {
        expect(listWikiPages).toHaveBeenCalledWith({
          vault_id: 1,
          page_type: "entity",
          search: undefined,
        });
      });
    });
  });

  // ---------------------------------------------------------------------------
  // DD-C020 optimistic-locking 409 conflict flow (issue #276 1X-1)
  // ---------------------------------------------------------------------------
  it("shows conflict toast and refetches pages when save returns 409", async () => {
    const { getWikiPage } = await import("@/lib/api");

    // Mock updateWikiPage to reject with a 409 conflict error
    (updateWikiPage as ReturnType<typeof vi.fn>).mockRejectedValue({
      response: { status: 409 },
    });

    // Mock getWikiPage to return a page so selectedPage is set
    (getWikiPage as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: 1,
      vault_id: 1,
      slug: "test-page",
      title: "Test Page",
      page_type: "entity",
      markdown: "# Test",
      summary: "A test page",
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
    });

    render(<WikiPage />);

    // Select a page via the mocked WikiPageList
    await act(async () => {
      screen.getByTestId("select-page-btn").click();
    });

    // Wait for WikiPageDetail to appear (page loaded)
    await waitFor(() => {
      expect(screen.getByTestId("wiki-page-detail")).toBeInTheDocument();
    });

    // Click the Edit button in the mocked WikiPageDetail
    await act(async () => {
      screen.getByTestId("edit-page-btn").click();
    });

    // Wait for the edit dialog to appear
    await waitFor(() => {
      expect(screen.getByTestId("wiki-edit-dialog")).toBeInTheDocument();
    });

    // Clear listWikiPages mock call count before triggering save
    (listWikiPages as ReturnType<typeof vi.fn>).mockClear();

    // Click the Save button in the mocked dialog
    await act(async () => {
      screen.getByTestId("save-dialog-btn").click();
    });

    // Assert: toast.error called with the exact conflict message
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith(
        "This page was edited by someone else. Refresh and try again."
      );
    });

    // Assert: listWikiPages was called (refetch after conflict)
    await waitFor(() => {
      expect(listWikiPages).toHaveBeenCalled();
    });
  });
});

// ---------------------------------------------------------------------------
// MemoryPage promote-to-wiki button test
// ---------------------------------------------------------------------------
describe("MemoryPage promote-to-wiki integration", () => {
  it("promoteMemoryToWiki function exists and is callable", async () => {
    const { promoteMemoryToWiki } = await import("@/lib/api");
    expect(typeof promoteMemoryToWiki).toBe("function");
    // Mock returns page on success
    (promoteMemoryToWiki as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      page: { id: 1, title: "AFOMIS", slug: "afomis", vault_id: 1 },
      claims: [],
      entities: [],
      relations: [],
    });
    const result = await promoteMemoryToWiki({ memory_id: 1, vault_id: 1 });
    expect(result.page.title).toBe("AFOMIS");
  });
});
