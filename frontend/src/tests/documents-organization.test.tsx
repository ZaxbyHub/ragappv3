/**
 * Phase 3 organization tests: document table sorting headers, tag filter,
 * and the bulk-selection hook.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, renderHook, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

// Render all virtual items so rows (and tag badges) are present.
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(({ count, estimateSize }: { count: number; estimateSize?: () => number }) => {
    const size = estimateSize?.() ?? 72;
    return {
      getVirtualItems: () =>
        Array.from({ length: count }, (_, i) => ({ index: i, start: i * size, size, key: i })),
      getTotalSize: () => count * size,
      measureElement: () => {},
    };
  }),
}));

// Radix Select cannot be driven in jsdom (no pointer-capture support), so we
// mock the primitive with a context that lets SelectItem clicks invoke
// onValueChange — exercising TagFilter's real value-mapping wiring.
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const Ctx = React.createContext<(v: string) => void>(() => {});
  return {
    Select: ({ onValueChange, children }: { onValueChange: (v: string) => void; children: React.ReactNode }) =>
      React.createElement(Ctx.Provider, { value: onValueChange }, children),
    SelectTrigger: ({ children, "aria-label": ariaLabel }: { children: React.ReactNode; "aria-label"?: string }) =>
      React.createElement("div", { role: "group", "aria-label": ariaLabel }, children),
    SelectValue: ({ placeholder }: { placeholder?: string }) => React.createElement("span", null, placeholder),
    SelectContent: ({ children }: { children: React.ReactNode }) => React.createElement("div", null, children),
    SelectItem: ({ value, children }: { value: string; children: React.ReactNode }) => {
      const onValueChange = React.useContext(Ctx);
      return React.createElement("button", { onClick: () => onValueChange(value) }, children);
    },
  };
});

import { DocumentTable } from "@/components/documents/DocumentTable";
import { TagFilter } from "@/components/documents/TagFilter";
import { useBulkSelection } from "@/components/documents/useBulkSelection";
import type { Document, Tag } from "@/lib/api";

const tag = (id: number, name: string): Tag => ({
  id,
  vault_id: 1,
  name,
  color: "",
  created_at: "",
  updated_at: "",
  document_count: 0,
});

const doc = (id: string, filename: string, tags: Tag[] = []): Document => ({
  id,
  filename,
  size: 100,
  created_at: "2024-01-01T00:00:00Z",
  metadata: { status: "indexed", chunk_count: 1 },
  tags,
});

function renderTable(overrides: Partial<Parameters<typeof DocumentTable>[0]> = {}) {
  const onSort = vi.fn();
  const props = {
    documents: [doc("1", "alpha.txt", [tag(5, "finance")])],
    selectedIds: new Set<string>(),
    canMutateDocuments: true,
    filenameColWidth: 250,
    onResizeMouseDown: vi.fn(),
    onSelectAll: vi.fn(),
    onSelectOne: vi.fn(),
    wikiStatusMap: {},
    compilingDocIds: new Set<string>(),
    onCompileDocument: vi.fn(),
    onDownload: vi.fn(),
    onDelete: vi.fn(),
    sortBy: "created_at" as const,
    sortOrder: "desc" as const,
    onSort,
    ...overrides,
  };
  render(
    <MemoryRouter>
      <DocumentTable {...props} />
    </MemoryRouter>
  );
  return { onSort };
}

describe("DocumentTable sorting", () => {
  it("calls onSort with the column when a sortable header is clicked", () => {
    const { onSort } = renderTable();
    fireEvent.click(screen.getByRole("button", { name: "Sort by Filename" }));
    expect(onSort).toHaveBeenCalledWith("file_name");
    fireEvent.click(screen.getByRole("button", { name: "Sort by Size" }));
    expect(onSort).toHaveBeenCalledWith("file_size");
    fireEvent.click(screen.getByRole("button", { name: "Sort by Uploaded" }));
    expect(onSort).toHaveBeenCalledWith("created_at");
    fireEvent.click(screen.getByRole("button", { name: "Sort by Status" }));
    expect(onSort).toHaveBeenCalledWith("status");
  });

  it("renders assigned tag badges and links the filename to the detail page", () => {
    renderTable();
    expect(screen.getByText("finance")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "alpha.txt" });
    expect(link).toHaveAttribute("href", "/documents/1");
  });
});

describe("TagFilter", () => {
  it("emits the tag id on selection and null for 'All tags'", () => {
    const onChange = vi.fn();
    render(<TagFilter tags={[tag(7, "ops")]} value={7} onChange={onChange} />);
    expect(screen.getByLabelText("Filter by tag")).toBeInTheDocument();

    // Selecting a tag emits its numeric id.
    fireEvent.click(screen.getByRole("button", { name: "ops (0)" }));
    expect(onChange).toHaveBeenCalledWith(7);

    // Selecting "All tags" emits null.
    fireEvent.click(screen.getByRole("button", { name: "All tags" }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("renders nothing when there are no tags", () => {
    const { container } = render(<TagFilter tags={[]} value={null} onChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("useBulkSelection", () => {
  beforeEach(() => vi.clearAllMocks());

  it("selects, toggles, and clears when enabled", () => {
    const { result } = renderHook(() => useBulkSelection(true));
    act(() => result.current.selectAll(["a", "b"]));
    expect(result.current.selectedIds).toEqual(new Set(["a", "b"]));
    act(() => result.current.selectOne("a", false));
    expect(result.current.selectedIds).toEqual(new Set(["b"]));
    act(() => result.current.clear());
    expect(result.current.selectedIds.size).toBe(0);
  });

  it("ignores mutations when disabled", () => {
    const { result } = renderHook(() => useBulkSelection(false));
    act(() => result.current.selectAll(["a", "b"]));
    expect(result.current.selectedIds.size).toBe(0);
    act(() => result.current.selectOne("a", true));
    expect(result.current.selectedIds.size).toBe(0);
  });
});
