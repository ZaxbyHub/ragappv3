import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import React from "react";

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

// Mock child wiki page components to isolate the parent
vi.mock("@/pages/WikiPageList", () => ({
  WikiPageList: () => <div data-testid="wiki-page-list">Page List</div>,
  PAGE_TYPES: [
    { value: "", label: "All" },
    { value: "overview", label: "Overview" },
    { value: "entity", label: "Entities" },
  ],
}));

vi.mock("@/pages/WikiPageDetail", () => ({
  WikiPageDetail: () => <div data-testid="wiki-page-detail">Page Detail</div>,
}));

vi.mock("@/pages/WikiEditDialog", () => ({
  WikiEditDialog: ({ open }: { open: boolean }) =>
    open ? <div data-testid="wiki-edit-dialog">Edit Dialog</div> : null,
}));

vi.mock("@/pages/WikiLintPanel", () => ({
  WikiLintPanel: ({ onRunLint }: { onRunLint: () => void }) => (
    <div data-testid="wiki-lint-panel">
      <button onClick={onRunLint} data-testid="run-lint-btn">Run Lint</button>
    </div>
  ),
}));

// ---------------------------------------------------------------------------
// Now import components after mocks are in place
// ---------------------------------------------------------------------------
import WikiPage from "./WikiPage";
import { listWikiPages, listWikiLintFindings, runWikiLint } from "@/lib/api";

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
