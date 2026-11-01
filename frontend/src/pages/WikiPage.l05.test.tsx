// frontend/src/pages/WikiPage.l05.test.tsx
// Issue #776 L05 / AC4 (frozen acceptance check) — every flex row between a
// header action button and the header root must be allowed to wrap on narrow
// viewports (flex-wrap or flex-col), so header actions reflow instead of
// overflowing.
//
// The check locates the "Jobs" header button, finds the header root (the
// nearest ancestor containing a descendant h1 — WikiPage.tsx:259, whose h1
// arrives via PageTitleHeader), and walks the ancestor chain up to AND
// INCLUDING that root counting rows that are flex, not flex-col and not
// flex-wrap. At master exactly one row qualifies: the action group
// "flex items-center gap-2" (WikiPage.tsx:263); the header root itself is
// base flex-col so it is excluded.
//
// Expected RED at master: "expected 1 to be +0".
//
// Harness: the full mock set from WikiPage.test.tsx (api factory mock,
// vault store, VaultSelector stub, sonner, radix-tabs stub, child page
// component stubs, never-resolving SSE fetch stub). jsdom has no layout
// engine — the assertion is class-token only.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
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

// Radix Tabs cannot be activated via fireEvent.click in jsdom — same stub
// approach as WikiPage.test.tsx.
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

// Mock child wiki page components to isolate the parent (WikiPage.test.tsx
// stubs — the header under test lives in WikiPage itself and is NOT stubbed).
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
  WikiEditDialog: () => null,
}));

vi.mock("@/pages/WikiLintPanel", () => ({
  WikiLintPanel: () => <div data-testid="wiki-lint-panel" />,
}));

// ---------------------------------------------------------------------------
// Now import components after mocks are in place
// ---------------------------------------------------------------------------
import WikiPage from "./WikiPage";
import { listWikiPages, listWikiLintFindings } from "@/lib/api";

// WikiPage opens an authenticated wiki-events fetch stream on mount — stub
// fetch with an open (never-resolving) stream (same as WikiPage.test.tsx).
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
            cancel: vi.fn(() => Promise.resolve()),
          }),
        },
      } as unknown as Response)
    )
  );
  (listWikiPages as ReturnType<typeof vi.fn>).mockResolvedValue({
    pages: [],
    page: 1,
    per_page: 50,
  });
  (listWikiLintFindings as ReturnType<typeof vi.fn>).mockResolvedValue({
    findings: [],
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("WikiPage L05 (issue #776)", () => {
  it("header action rows wrap", async () => {
    await act(async () => {
      render(<WikiPage />);
    });

    const button = screen.getByRole("button", { name: /jobs/i });

    // Header root = the nearest ancestor of the button that contains a
    // descendant h1 (WikiPage renders its h1 via PageTitleHeader).
    let headerRoot: HTMLElement | null = null;
    for (
      let el: HTMLElement | null = button.parentElement;
      el instanceof HTMLElement;
      el = el.parentElement
    ) {
      if (el.querySelector("h1")) {
        headerRoot = el;
        break;
      }
    }
    expect(headerRoot).not.toBeNull();

    // Walk from the button's ancestors up to AND INCLUDING the header root;
    // count flex rows that are neither flex-col nor flex-wrap.
    let unwrappedRows = 0;
    const stop = (headerRoot as HTMLElement).parentElement;
    for (
      let el: HTMLElement | null = button.parentElement;
      el instanceof HTMLElement && el !== stop;
      el = el.parentElement
    ) {
      const cls = el.getAttribute("class") ?? "";
      if (cls.includes("flex") && !cls.includes("flex-col") && !cls.includes("flex-wrap")) {
        unwrappedRows += 1;
      }
    }

    expect(unwrappedRows).toBe(0);
  });
});
