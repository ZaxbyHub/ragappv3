// frontend/src/pages/WikiPage.m02.test.tsx
// Issue-trace 782-firstrun-checklist-vaultgate-kms-wiki — acceptance check C2 (AC2).
//
// The Wiki no-selection branch (WikiPage.tsx:318-322) renders a bare
// EmptyState (title "Select a vault") with NO selector inside it — the only
// VaultSelector is the header one (WikiPage.tsx:243), outside the empty
// state. This check counts selectors WITHIN the empty-state root that
// carries the "Select a vault" title only.
//
// Expected pre-fix (base) verdict: RED with "expected +0 to be 1".
//
// Mock idioms reused from frontend/src/pages/WikiPage.test.tsx verbatim (the
// @/lib/api factory incl. the event-stream helpers, the child page-component
// mocks, the VaultSelector data-testid stub, and the fetch-stub
// beforeEach/afterEach that keeps useWikiEventStream's never-resolving stream
// from making real requests), with two adjustments:
// - useVaultStore is a vi.fn() so the test can seed activeVaultId: null with
//   accessible vaults (the first-run scenario).
// - The ui/tabs mock is dropped: with no active vault the toolbar (and its
//   Tabs) never mounts, and no tab interaction is driven here.
//
// The real EmptyState component is used (not mocked): its root is the
// div[role="status"] the title lives in, which scopes the selector count.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import React from "react";

// ---------------------------------------------------------------------------
// Mock API module — declared before any component imports (WikiPage.test.tsx)
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

// useVaultStore as a mockable fn so the test can seed the first-run scenario
// (accessible vaults exist, none selected).
vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: 1,
    vaults: [],
  })),
}));

// Mock VaultSelector (WikiPage.test.tsx stub, same data-testid as the m01
// fixtures — counts the selector wherever it renders).
vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector">VaultSelector</div>,
}));

// Mock sonner
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

// Mock child wiki page components to isolate the parent (WikiPage.test.tsx)
vi.mock("@/pages/WikiPageList", () => ({
  WikiPageList: ({ onSelect }: { onSelect?: (pageId: number) => void }) => (
    <div data-testid="wiki-page-list">
      Page List
      <button data-testid="select-page-btn" onClick={() => onSelect?.(1)}>
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
import { useVaultStore } from "@/stores/useVaultStore";

// WikiPage opens an authenticated wiki-events fetch stream on mount
// (useWikiEventStream). Stub fetch with an open (never-resolving) stream so
// these rendering tests make no real request and trigger no reconnect.
// (beforeEach/afterEach pattern copied from WikiPage.test.tsx.)
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

describe("WikiPage m02 (issue-trace 782-firstrun-checklist-vaultgate-kms-wiki)", () => {
  it("no-selection empty state uses the VaultGate selector", async () => {
    // First-run scenario: an accessible vault exists, none is selected.
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: null,
      vaults: [{ id: 2, name: "Team Vault", current_user_permission: "write" }],
    } as ReturnType<typeof useVaultStore>);

    await act(async () => {
      render(<WikiPage />);
    });

    // The no-selection empty state is rendered under its "Select a vault" title.
    const title = screen.getByText("Select a vault");
    expect(title).toBeInTheDocument();

    // The empty-state root is the real EmptyState's div[role="status"]; the
    // header VaultSelector (WikiPage.tsx:243) is OUTSIDE it, so it cannot
    // satisfy the count.
    const emptyStateRoot = title.closest('div[role="status"]');
    expect(emptyStateRoot).not.toBeNull();

    // Exactly one selector must be offered WITHIN the empty state.
    expect(
      within(emptyStateRoot as HTMLElement).queryAllByTestId("vault-selector").length
    ).toBe(1);
  });
});
