import { describe, it, expect, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, fireEvent, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

// Mock API module before importing the component.
vi.mock("@/lib/api", () => ({
  bulkWikiPageAction: vi.fn().mockResolvedValue({}),
}));

// Radix Tabs cannot be activated via fireEvent.click in jsdom (pointer-capture /
// activation semantics). Mock the primitive so TabsTrigger is a plain button that
// invokes onValueChange — same approach the repo uses for ui/tabs and ui/select
// (see frontend-testing-gotchas + RightPane.test.tsx). WikiPageList is the sole
// ui/tabs consumer in this file, so reshaping the module here is safe.
vi.mock("@/components/ui/tabs", async () => {
  const React = await import("react");
  const Ctx = React.createContext<(v: string) => void>(() => {});
  return {
    Tabs: ({ onValueChange, children }: any) =>
      React.createElement(Ctx.Provider, { value: onValueChange }, children),
    TabsList: ({ children }: any) => React.createElement("div", null, children),
    TabsTrigger: ({ value, children }: any) => {
      const onValueChange = React.useContext(Ctx);
      return React.createElement(
        "button",
        { role: "tab", onClick: () => onValueChange(value) },
        children
      );
    },
  };
});

import { WikiPageList } from "./WikiPageList";
import { bulkWikiPageAction } from "@/lib/api";
import type { WikiPage } from "@/lib/api";

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

beforeEach(() => {
  vi.clearAllMocks();
});

const makePage = (overrides: Partial<WikiPage> = {}): WikiPage => ({
  id: 1,
  vault_id: 1,
  slug: "page-one",
  title: "Page One",
  page_type: "entity",
  markdown: "",
  summary: "First page summary",
  status: "draft",
  confidence: 0.9,
  created_by: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  last_compiled_at: null,
  claims: [],
  entities: [],
  lint_findings: [],
  ...overrides,
});

const defaultProps = () => ({
  pages: [
    makePage({ id: 1, title: "Page One", slug: "page-one" }),
    makePage({ id: 2, title: "Page Two", slug: "page-two", status: "verified" }),
  ],
  loading: false,
  onSelect: vi.fn(),
  onFilter: vi.fn(),
  onCreateClick: vi.fn(),
  vaultId: 7,
});

describe("WikiPageList", () => {
  it("renders a row for each page", () => {
    render(<WikiPageList {...defaultProps()} />);
    expect(screen.getByText("Page One")).toBeInTheDocument();
    expect(screen.getByText("Page Two")).toBeInTheDocument();
  });

  it("shows empty state when there are no pages and not loading", () => {
    render(<WikiPageList {...defaultProps()} pages={[]} />);
    expect(screen.getByText("No pages found.")).toBeInTheDocument();
  });

  it("calls onSelect with the page id when a row is clicked", () => {
    const props = defaultProps();
    render(<WikiPageList {...props} />);
    fireEvent.click(screen.getByText("Page One"));
    expect(props.onSelect).toHaveBeenCalledWith(1);
  });

  it("selecting a row reveals the bulk action bar", () => {
    render(<WikiPageList {...defaultProps()} />);
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Select Page One"));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("select-all checkbox selects every page", () => {
    render(<WikiPageList {...defaultProps()} />);
    fireEvent.click(screen.getByLabelText("Select all pages"));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
  });

  it("Delete with confirm=true calls bulkWikiPageAction(vaultId, ids, 'delete')", async () => {
    (window.confirm as ReturnType<typeof vi.fn>).mockReturnValueOnce(true);
    render(<WikiPageList {...defaultProps()} />);
    fireEvent.click(screen.getByLabelText("Select Page One"));
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    await waitFor(() => {
      expect(bulkWikiPageAction).toHaveBeenCalledWith(7, [1], "delete");
    });
  });

  it("Delete with confirm=false does NOT call bulkWikiPageAction", () => {
    (window.confirm as ReturnType<typeof vi.fn>).mockReturnValueOnce(false);
    render(<WikiPageList {...defaultProps()} />);
    fireEvent.click(screen.getByLabelText("Select Page One"));
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    expect(bulkWikiPageAction).not.toHaveBeenCalled();
  });

  it("Set Draft calls bulkWikiPageAction(vaultId, ids, 'update', { status: 'draft' })", async () => {
    render(<WikiPageList {...defaultProps()} />);
    fireEvent.click(screen.getByLabelText("Select Page One"));
    fireEvent.click(screen.getByRole("button", { name: /set draft/i }));
    await waitFor(() => {
      expect(bulkWikiPageAction).toHaveBeenCalledWith(7, [1], "update", { status: "draft" });
    });
  });

  it("Archive calls bulkWikiPageAction with status 'archived'", async () => {
    render(<WikiPageList {...defaultProps()} />);
    fireEvent.click(screen.getByLabelText("Select Page One"));
    fireEvent.click(screen.getByRole("button", { name: /archive/i }));
    await waitFor(() => {
      expect(bulkWikiPageAction).toHaveBeenCalledWith(7, [1], "update", { status: "archived" });
    });
  });

  it("Clear deselects all and hides the bulk bar", () => {
    render(<WikiPageList {...defaultProps()} />);
    fireEvent.click(screen.getByLabelText("Select Page One"));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /clear/i }));
    expect(screen.queryByText("1 selected")).not.toBeInTheDocument();
  });

  it("search submit (button) calls onFilter with the query", () => {
    const props = defaultProps();
    render(<WikiPageList {...props} />);
    fireEvent.change(screen.getByPlaceholderText("Search wiki..."), {
      target: { value: "alpha" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(props.onFilter).toHaveBeenCalledWith({ page_type: undefined, search: "alpha" });
  });

  it("search submit on Enter calls onFilter with the query", () => {
    const props = defaultProps();
    render(<WikiPageList {...props} />);
    const input = screen.getByPlaceholderText("Search wiki...");
    fireEvent.change(input, { target: { value: "beta" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(props.onFilter).toHaveBeenCalledWith({ page_type: undefined, search: "beta" });
  });

  it("changing the activeType tab fires onFilter with the page_type", async () => {
    const props = defaultProps();
    render(<WikiPageList {...props} />);
    props.onFilter.mockClear();
    fireEvent.click(screen.getByRole("tab", { name: "Entities" }));
    await waitFor(() => {
      expect(props.onFilter).toHaveBeenCalledWith({ page_type: "entity", search: undefined });
    });
  });

  it("changing the pages prop clears the current selection", () => {
    const props = defaultProps();
    const { rerender } = render(<WikiPageList {...props} />);
    fireEvent.click(screen.getByLabelText("Select Page One"));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    // rerender re-injects into the same MemoryRouter wrapper supplied by render().
    rerender(
      <WikiPageList {...props} pages={[makePage({ id: 3, title: "Page Three", slug: "page-three" })]} />
    );
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
  });
});
