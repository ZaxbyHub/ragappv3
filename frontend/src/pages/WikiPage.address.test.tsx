import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { useLocation } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";

// #1161 AC1 (frozen check C1): the `?page=` URL param is the single owner
// of the open wiki page — a list click writes it, the effect opens once per
// distinct value, Back removes it and closes the detail.

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "http://test",
  getJwtAccessToken: vi.fn(() => "token"),
  refreshAccessToken: vi.fn(async () => "token"),
  getWikiActivityFeed: vi.fn(async () => ({ entries: [] })),
  listWikiPages: vi.fn(async () => ({ pages: [], page: 1, per_page: 50, total: 0 })),
  listWikiEntities: vi.fn(async () => ({ entities: [] })),
  searchWiki: vi.fn(async () => ({ results: [] })),
  getWikiPage: vi.fn(async () => ({
    id: 7,
    vault_id: 1,
    slug: "document/one",
    title: "Deep Linked Page",
    page_type: "entity",
    markdown: "body",
    summary: "",
    status: "needs_review",
    confidence: 0,
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    last_compiled_at: null,
  })),
  listWikiLintFindings: vi.fn(async () => ({ findings: [] })),
  runWikiLint: vi.fn(async () => ({ findings: [], count: 0 })),
  resolveWikiLintFinding: vi.fn(async () => ({})),
  listWikiClaims: vi.fn(async () => ({ claims: [] })),
  createWikiPage: vi.fn(async () => ({})),
  updateWikiPage: vi.fn(async () => ({})),
  deleteWikiPage: vi.fn(async () => ({})),
  bulkWikiPageAction: vi.fn(async () => ({})),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: () => ({ activeVaultId: 1 }),
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

// The list stub surfaces the onSelect prop so the spec can drive a list
// click without depending on WikiPageList internals.
vi.mock("@/pages/WikiPageList", () => {
  const StubList = ({ onSelect }: { onSelect: (id: number) => void }) => (
    <div data-testid="wiki-list">
      <button type="button" onClick={() => onSelect(7)}>
        Open page seven
      </button>
    </div>
  );
  return {
    PAGE_TYPES: [],
    WikiPageList: StubList,
    default: StubList,
  };
});

vi.mock("@/pages/WikiPageDetail", () => {
  const StubDetail = ({ onBack }: { onBack: () => void }) => (
    <div>
      <h2>Detail Open</h2>
      <button type="button" onClick={onBack}>
        Back
      </button>
    </div>
  );
  return { WikiPageDetail: StubDetail, default: StubDetail };
});

vi.mock("@/pages/WikiEditDialog", () => {
  const StubDialog = () => null;
  return { WikiEditDialog: StubDialog, default: StubDialog };
});

vi.mock("@/pages/WikiLintPanel", () => {
  const StubPanel = () => <div data-testid="wiki-lint-panel" />;
  return { WikiLintPanel: StubPanel, default: StubPanel };
});

vi.mock("@/pages/WikiJobsPanel", () => {
  const StubJobs = () => <div data-testid="wiki-jobs-panel" />;
  return { WikiJobsPanel: StubJobs, default: StubJobs };
});

const WikiPage = (await import("@/pages/WikiPage")).default;
const { getWikiPage, listWikiPages } = await import("@/lib/api");

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.search}</div>;
}

function renderAt(initial: string) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <LocationProbe />
      <WikiPage />
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // WikiPage opens an authenticated wiki-events fetch stream on mount
  // (useWikiEventStream). Stub fetch with a never-resolving stream so no
  // real request is made (the exemplar WikiPage.test.tsx pattern).
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
});

describe("WikiPage address deep linking (#1161 AC1 / C1)", () => {
  it("puts the page's identifier in the address when a list row opens it", async () => {
    const user = userEvent.setup();
    renderAt("/wiki");
    await waitFor(() => {
      expect(listWikiPages).toHaveBeenCalled();
    });
    await user.click(await screen.findByText("Open page seven"));
    // The address now carries the page identifier.
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("page=7");
    });
    // Single owner: the param effect performs exactly ONE detail fetch.
    await waitFor(() => {
      expect(getWikiPage).toHaveBeenCalledTimes(1);
    });
    expect(getWikiPage).toHaveBeenCalledWith(7);
    expect(await screen.findByText("Detail Open")).toBeInTheDocument();
  });

  it("restores a deep link (?page=<id>) on load", async () => {
    renderAt("/wiki?page=7");
    await waitFor(() => {
      expect(getWikiPage).toHaveBeenCalledWith(7);
    });
    expect(await screen.findByText("Detail Open")).toBeInTheDocument();
  });

  it("Back removes the param and closes the detail without reopening", async () => {
    const user = userEvent.setup();
    renderAt("/wiki?page=7");
    await waitFor(() => {
      expect(getWikiPage).toHaveBeenCalledTimes(1);
    });
    await user.click(await screen.findByText("Back"));
    await waitFor(() => {
      expect(screen.getByTestId("location")).not.toHaveTextContent("page=7");
    });
    await waitFor(() => {
      expect(screen.queryByText("Detail Open")).not.toBeInTheDocument();
    });
    // No reopen after Back: the fetch count did not grow.
    expect(getWikiPage).toHaveBeenCalledTimes(1);
  });

  it("does not re-fetch when the same page is selected again", async () => {
    const user = userEvent.setup();
    renderAt("/wiki");
    await user.click(await screen.findByText("Open page seven"));
    await waitFor(() => {
      expect(getWikiPage).toHaveBeenCalledTimes(1);
    });
    // A re-render with the same param must not duplicate the fetch.
    await user.click(await screen.findByText("Open page seven"));
    await new Promise((r) => setTimeout(r, 50));
    expect(getWikiPage).toHaveBeenCalledTimes(1);
  });
});
