// Issue 258 / AC22 (ENH-011) — context-specific skeleton aria-labels.
//
// Phase 2.5 acceptance check. At base the shared `Skeleton` primitive
// (src/components/ui/skeleton.tsx) is the ONLY thing carrying
// role="status" + aria-label, and its label is the generic "Loading...".
// Every composite skeleton (DocumentsTableSkeleton, CanvasPageSkeleton,
// DraftRoomDetailSkeleton, DraftListSkeleton, the ManageGroups/ManageOrgs
// sheet loading lists, and App.tsx's PageLoader) therefore announces the
// same generic string — a screen-reader user cannot tell WHAT is loading.
//
// This check renders each composite in its real loading state and asserts
// it exposes a CONTEXT-SPECIFIC role="status" label:
//   documents table → /loading documents/i
//   canvas page     → /loading canvas/i
//   draft detail    → /loading draft/i
//   draft list      → /loading draft/i (matches "Loading drafts…")
//   groups sheet    → /loading group/i
//   orgs sheet      → /loading org/i  (matches "Loading organizations…")
//   App PageLoader  → role="status" + a /loading/i label (component-level;
//                     PageLoader is module-private and only reachable
//                     through the full App shell, so this one assertion is
//                     source-scoped to the PageLoader function body — the
//                     sanctioned pattern in src/pages/LoginPage.test.tsx)
//
// At base every behavioral query finds only generic "Loading..." labels
// (and PageLoader has neither role nor label) → RED across the board.
// Phase 4 adds the context labels; nothing here is weakened (generic inner
// primitives may remain provided a context-labeled status exists —
// getAllByRole accepts one-or-more matches).

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { DocumentsTableSkeleton } from "@/components/documents/DocumentsTableSkeleton";
import CanvasPage from "@/components/canvas/CanvasPage";
import DraftRoomDetailPage from "@/pages/DraftRoomDetailPage";
import DraftRoomPage from "@/pages/DraftRoomPage";
import { ManageGroupsSheet } from "@/pages/AdminUsersPage/ManageGroupsSheet";
import { ManageOrgsSheet } from "@/pages/AdminUsersPage/ManageOrgsSheet";
import type { Group, OrgItem, User } from "@/pages/AdminUsersPage/types";

// Radix Checkbox (DocumentsTableSkeleton) and ScrollArea (sheets) need it.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

// vi.mock factories are hoisted above module-level bindings, so the
// never-resolving fetchers are inlined per factory below.
// Keep the real modules (keys, error helpers, types) and only freeze the
// data fetchers at pending so the pages stay in their genuine loading state.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listAccessibleVaults: vi.fn(() => new Promise<never>(() => {})),
  };
});

vi.mock("@/lib/api/draftRoom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/draftRoom")>();
  return {
    ...actual,
    listDrafts: vi.fn(() => new Promise<never>(() => {})),
    getDraft: vi.fn(() => new Promise<never>(() => {})),
    getDraftRevision: vi.fn(() => new Promise<never>(() => {})),
  };
});

vi.mock("@/lib/api/canvas", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/canvas")>();
  return {
    ...actual,
    getCanvasArtifact: vi.fn(() => new Promise<never>(() => {})),
  };
});

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: true, isError: false }),
  useDraftRoomVisible: () => false,
}));

vi.mock("@/hooks/useCanvasCapabilities", () => ({
  useCanvasCapabilities: () => ({ data: undefined, isLoading: true, isError: false }),
}));

vi.mock("@/hooks/useDraftRoomEvents", () => ({
  useDraftRoomEvents: () => ({ pollingFallback: false }),
}));

function renderAt(path: string, routePath: string, element: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={routePath} element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function expectContextStatus(labelRegex: RegExp, surface: string) {
  console.log(`AC22 CHECK: FAIL — expecting a role="status" labeled ${labelRegex} on the ${surface}`);
  // getAllByRole throws (check RED) when zero matches; multiple labeled
  // statuses are acceptable (e.g. one wrapper status plus primitives).
  const matches = screen.getAllByRole("status", { name: labelRegex });
  expect(matches.length).toBeGreaterThanOrEqual(1);
}

const testUser: User = {
  id: 1,
  username: "alice",
  full_name: "Alice Example",
  role: "admin",
  is_active: true,
  created_at: "2026-01-01T00:00:00Z",
};
const groups: Group[] = [{ id: 1, name: "Research", description: null }];
const orgs: OrgItem[] = [{ id: 1, name: "Acme", description: "acme org" }];

describe("AC22 — context-specific skeleton aria-labels (ENH-011)", () => {
  it("AC22: DocumentsTableSkeleton announces /loading documents/i", () => {
    render(<DocumentsTableSkeleton />);
    expectContextStatus(/loading documents/i, "documents table skeleton");
  });

  it("AC22: CanvasPage loading announces /loading canvas/i", () => {
    renderAt("/chat/s1/canvas/artifact-uid-1", "/chat/:sessionId/canvas/:artifactUid", <CanvasPage />);
    expectContextStatus(/loading canvas/i, "canvas page skeleton");
  });

  it("AC22: DraftRoomDetailPage loading announces /loading draft/i", () => {
    renderAt("/draft-room/42", "/draft-room/:draftId", <DraftRoomDetailPage />);
    expectContextStatus(/loading draft/i, "draft detail skeleton");
  });

  it("AC22: DraftRoomPage loading announces /loading draft/i", () => {
    renderAt("/draft-room", "/draft-room", <DraftRoomPage />);
    expectContextStatus(/loading draft/i, "draft list skeleton");
  });

  it("AC22: ManageGroupsSheet loading announces /loading group/i", () => {
    render(
      <ManageGroupsSheet
        open
        user={testUser}
        allGroups={groups}
        selectedGroupIds={[]}
        isLoading
        isSaving={false}
        searchQuery=""
        onSearchChange={() => {}}
        onToggleGroup={() => {}}
        onSave={async () => {}}
        onClose={() => {}}
      />
    );
    expectContextStatus(/loading group/i, "manage-groups sheet loading list");
  });

  it("AC22: ManageOrgsSheet loading announces /loading org/i", () => {
    render(
      <ManageOrgsSheet
        open
        user={testUser}
        allOrgs={orgs}
        orgMemberships={new Map()}
        isLoading
        isSaving={false}
        searchQuery=""
        onSearchChange={() => {}}
        onToggleOrg={() => {}}
        onSetOrgRole={() => {}}
        onSave={async () => {}}
        onClose={() => {}}
      />
    );
    expectContextStatus(/loading org/i, "manage-orgs sheet loading list");
  });

  it("AC22: App PageLoader exposes role=status with a /loading/i label", () => {
    // Component-level, source-scoped: PageLoader is not exported and is
    // only reachable via App's Suspense fallback, which cannot be captured
    // deterministically in jsdom (the lazy chunk resolves in a microtask).
    // Scoped to the PageLoader function body so a role/label elsewhere in
    // App.tsx cannot satisfy this.
    const appSource = readFileSync(resolve(__dirname, "../App.tsx"), "utf-8");
    const start = appSource.indexOf("function PageLoader");
    const end = appSource.indexOf("function MainAppShell");
    const block = start >= 0 && end > start ? appSource.slice(start, end) : "";

    console.log(
      "AC22 CHECK: FAIL — expecting App PageLoader to carry role=\"status\" and a /loading/i aria-label"
    );
    expect(block.length).toBeGreaterThan(0);
    expect(block).toMatch(/role=["']status["']/);
    expect(block).toMatch(/aria-label=["'][^"']*loading/i);
  });
});
