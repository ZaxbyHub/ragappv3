/**
 * ChatShell ?document_ids= deep link (issue #514 AC-23).
 *
 * DocumentDetailPage routes to /chat?document_ids=<id> AND sets the scope
 * through the chat-mode store. The store is the source of truth at send time;
 * on a HARD refresh (store empty) ChatShell restores the scope from the URL
 * once on mount and never overrides a scope that already exists (the store
 * always wins). Garbage params must leave the scope untouched.
 *
 * Mock scaffolding mirrors ChatShell.test.tsx (matchMedia, Sheet,
 * useChatShellStore, getChatSession override) so the heavy ChatShell tree
 * mounts without network. The chat-mode store is intentionally NOT mocked —
 * the deep-link effect must talk to the real store the composer reads.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ChatShell from "./ChatShell";
import { useChatModeStore } from "@/stores/useChatModeStore";

// Mock matchMedia for useIsMobile hook. Default is desktop (no media query
// matches) — identical to ChatShell.test.tsx.
let matchMediaMatches = false;
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    get matches() {
      return matchMediaMatches;
    },
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// Mutable mock store state — getState is attached to the hook itself
// (zustand shape) so ChatShell's useChatShellStore.getState() works.
let mockStoreState = {
  sessionRailOpen: false,
  rightPaneOpen: false,
  rightPaneWidth: 320,
  sessionRailWidth: 280,
  activeSessionId: null as string | null,
  activeSessionTitle: null as string | null,
  sessionListRefreshToken: 0,
  activeRightTab: "evidence" as "evidence" | "preview",
  sessionSearchQuery: "",
  pinnedSessionIds: [] as number[],
  selectedEvidenceSource: null,
  selectedEvidenceMessageId: null as string | null,
  evidenceReturnFocusId: null as string | null,
  mobileSheetOpen: false,
  setMobileSheetOpen: vi.fn(),
  toggleSessionRail: vi.fn(),
  toggleRightPane: vi.fn(),
  setRightPaneWidth: vi.fn(),
  setSessionRailWidth: vi.fn(),
  setActiveSessionId: vi.fn(),
  setActiveSessionTitle: vi.fn(),
  requestSessionListRefresh: vi.fn(),
  openSessionRail: vi.fn(),
  closeSessionRail: vi.fn(),
  openRightPane: vi.fn(),
  closeRightPane: vi.fn(),
  setActiveRightTab: vi.fn(),
  setSessionSearchQuery: vi.fn(),
  togglePinSession: vi.fn(),
  isSessionPinned: vi.fn(),
  setSelectedEvidenceSource: vi.fn(),
  resetEvidenceSelection: vi.fn(),
  getState: () => mockStoreState,
};

vi.mock("@/components/ui/sheet", () => ({
  Sheet: ({ children, open }: { children: React.ReactNode; open?: boolean }) => (
    <div data-testid="sheet-mock" data-open={open ? "true" : "false"}>{open ? children : null}</div>
  ),
  SheetContent: ({ children, side }: { children: React.ReactNode; className?: string; side?: string; overlay?: boolean }) => (
    <div data-testid="sheet-content" data-side={side}>{children}</div>
  ),
  SheetHeader: ({ children }: { children: React.ReactNode; className?: string }) => (
    <div data-testid="sheet-header">{children}</div>
  ),
  SheetTitle: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="sheet-title">{children}</div>
  ),
  SheetDescription: ({ children }: { children: React.ReactNode }) => (
    <p data-testid="sheet-description">{children}</p>
  ),
  SheetClose: ({ children, onClick }: { children?: React.ReactNode; onClick?: () => void }) => (
    <button data-testid="sheet-close" onClick={onClick}>{children}</button>
  ),
}));

vi.mock("@/stores/useChatShellStore", () => {
  const makeHook = () => {
    const hook = vi.fn(() => mockStoreState);
    (hook as unknown as { getState: () => typeof mockStoreState }).getState = () => mockStoreState;
    return hook;
  };
  const hook = makeHook();
  return { __esModule: true, default: hook, useChatShellStore: hook };
});

// Override ONLY getChatSession so a transcript fetch can never hit the
// network; everything else in the api barrel stays real (as in ChatShell.test.tsx).
const chatShellGetChatSession = vi.hoisted(() =>
  vi.fn(async () => ({ messages: [] }))
);
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, getChatSession: chatShellGetChatSession };
});

function renderChatShellAt(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ChatShell />
    </MemoryRouter>
  );
}

describe("ChatShell ?document_ids= deep link (issue #514 AC-23)", () => {
  beforeEach(() => {
    mockStoreState = {
      ...mockStoreState,
      sessionRailOpen: false,
      rightPaneOpen: false,
      activeSessionId: null,
      activeSessionTitle: null,
      selectedEvidenceSource: null,
      selectedEvidenceMessageId: null,
      evidenceReturnFocusId: null,
      mobileSheetOpen: false,
      getState: () => mockStoreState,
    };
    matchMediaMatches = false;
    vi.clearAllMocks();
    // The chat-mode store persists across renders — reset the one-shot scope
    // so each case starts from the "hard refresh" (store empty) baseline.
    useChatModeStore.setState({ scopeDocumentIds: null });
  });

  it("restores the document scope from the URL on a hard refresh (store empty)", () => {
    renderChatShellAt("/chat?document_ids=7");

    expect(useChatModeStore.getState().scopeDocumentIds).toEqual([7]);
  });

  it("never overrides an existing store scope — the store wins over the param", () => {
    useChatModeStore.setState({ scopeDocumentIds: [9] });

    renderChatShellAt("/chat?document_ids=7");

    expect(useChatModeStore.getState().scopeDocumentIds).toEqual([9]);
  });

  it("leaves the scope null when the param is garbage (non-numeric ids)", () => {
    renderChatShellAt("/chat?document_ids=abc");

    expect(useChatModeStore.getState().scopeDocumentIds).toBeNull();
  });
});
