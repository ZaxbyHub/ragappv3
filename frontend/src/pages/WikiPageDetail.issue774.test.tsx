/**
 * Issue #774 regression tests (non-frozen): WikiPageDetail's collapsible
 * sections (version history / attachments / backlinks) must render a failure
 * line when their fetch rejects, not the affirmative empty text ("No version
 * history available." etc.).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";
import { WikiPageDetail } from "./WikiPageDetail";
import type { WikiPage } from "@/lib/api";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getWikiPageVersions: vi.fn().mockRejectedValue(new Error("versions down")),
    getWikiPageFiles: vi.fn().mockRejectedValue(new Error("files down")),
    getWikiPageBacklinks: vi.fn().mockRejectedValue(new Error("backlinks down")),
  };
});

import { getWikiPageVersions, getWikiPageFiles, getWikiPageBacklinks } from "@/lib/api";

function makePage(): WikiPage {
  return {
    id: 10,
    vault_id: 2,
    slug: "doc/alice",
    title: "Alice doc",
    page_type: "entity",
    summary: "",
    markdown: "",
    status: "draft",
    confidence: 0,
    created_by: null,
    created_at: "2024-01-01T00:00:00",
    updated_at: "2024-01-01T00:00:00",
    last_compiled_at: null,
    claims: [],
    entities: [],
    lint_findings: [],
  } as unknown as WikiPage;
}

beforeEach(() => {
  vi.clearAllMocks();
  // Re-arm the rejections after clearAllMocks resets mock implementations set
  // at module scope is unnecessary here (mockRejectedValue persists), but
  // clearAllMocks clears call history only — keep implementations intact.
});

function renderDetail() {
  render(
    <MemoryRouter>
      <WikiPageDetail page={makePage()} onBack={() => {}} onEdit={() => {}} onDelete={() => {}} />
    </MemoryRouter>
  );
}

function openSection(name: RegExp) {
  const header = screen.getByText(name).closest("div[class*='cursor-pointer']");
  expect(header).toBeTruthy();
  fireEvent.click(header!);
}

describe("issue 774 WikiPageDetail section failure states", () => {
  it("shows a failure line, not the empty verdict, when versions fail to load", async () => {
    renderDetail();
    openSection(/Version History/i);

    await waitFor(() => expect(getWikiPageVersions).toHaveBeenCalled());
    expect(await screen.findByRole("alert", {}, { timeout: 3000 })).toHaveTextContent(
      /failed to load version history/i
    );
    expect(screen.queryByText("No version history available.")).not.toBeInTheDocument();
  });

  it("shows a failure line, not the empty verdict, when attachments fail to load", async () => {
    renderDetail();
    openSection(/Attachments/i);

    await waitFor(() => expect(getWikiPageFiles).toHaveBeenCalled());
    expect(await screen.findByRole("alert", {}, { timeout: 3000 })).toHaveTextContent(
      /failed to load attachments/i
    );
    expect(screen.queryByText("No attachments.")).not.toBeInTheDocument();
  });

  it("shows a failure line, not the empty verdict, when backlinks fail to load", async () => {
    renderDetail();
    openSection(/Backlinks/i);

    await waitFor(() => expect(getWikiPageBacklinks).toHaveBeenCalled());
    expect(await screen.findByRole("alert", {}, { timeout: 3000 })).toHaveTextContent(
      /failed to load backlinks/i
    );
    expect(screen.queryByText("No pages link to this page.")).not.toBeInTheDocument();
  });
});
