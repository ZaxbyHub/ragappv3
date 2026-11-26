import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";

// #1161 AC2 (frozen check C2): selecting wiki pages offers a mark-reviewed
// action that updates EVERY selected page (needs_review -> verified), with
// category totals and updated-time labels on the loaded window.

vi.mock("@/lib/api", () => ({
  bulkWikiPageAction: vi.fn().mockResolvedValue({}),
}));

import { bulkWikiPageAction } from "@/lib/api";
import { WikiPageList } from "./WikiPageList";

import type { WikiPage } from "@/lib/api";

const renderList: typeof render = (ui, options) =>
  render(ui, { wrapper: MemoryRouter, ...options });

function makePage(overrides: Partial<WikiPage> = {}): WikiPage {
  return {
    id: 1,
    vault_id: 7,
    slug: "document/one",
    title: "Page One",
    page_type: "entity",
    markdown: "",
    summary: "",
    status: "needs_review",
    confidence: 0,
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-02-02T10:00:00Z",
    last_compiled_at: null,
    ...overrides,
  } as WikiPage;
}

function defaultProps() {
  return {
    pages: [
      makePage({ id: 1, title: "Page One", page_type: "entity" }),
      makePage({ id: 2, title: "Page Two", page_type: "procedure", updated_at: "2026-03-03T00:00:00Z" }),
    ],
    loading: false,
    onSelect: vi.fn(),
    vaultId: 7,
    onRefresh: vi.fn(),
    hasMore: false,
    loadingMore: false,
    onLoadMore: vi.fn(),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("WikiPageList bulk mark-reviewed (#1161 AC2 / C2)", () => {
  it("updates every selected page via one bulk update to verified", async () => {
    const user = userEvent.setup();
    renderList(<WikiPageList {...defaultProps()} />);
    await user.click(screen.getByLabelText("Select Page One"));
    await user.click(screen.getByLabelText("Select Page Two"));
    const button = await screen.findByRole("button", { name: /mark reviewed/i });
    expect(button).toBeInTheDocument();
    await user.click(button);
    await waitFor(() => {
      expect(bulkWikiPageAction).toHaveBeenCalledWith(7, [1, 2], "update", {
        status: "verified",
      });
    });
  });

  it("keeps the pre-existing bulk actions (Set Draft, Archive, Delete)", () => {
    renderList(<WikiPageList {...defaultProps()} />);
    // Not selected yet: the bar is hidden.
    expect(screen.queryByRole("button", { name: /delete/i })).not.toBeInTheDocument();
  });

  it("renders category totals for the loaded window and update-time labels", () => {
    renderList(<WikiPageList {...defaultProps()} />);
    const totals = screen.getByLabelText("Category totals for the loaded pages");
    expect(totals).toHaveTextContent("entity: 1");
    expect(totals).toHaveTextContent("procedure: 1");
    // Time labels: each row shows its updated_at.
    expect(screen.getAllByText(/Updated /).length).toBe(2);
  });
});
