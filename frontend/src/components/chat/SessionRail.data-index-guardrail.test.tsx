// frontend/src/components/chat/SessionRail.data-index-guardrail.test.tsx
// Trace meridian-canvas-401-vault-zero — acceptance check C4 (AC4, RC3).
//
// PRESERVING guardrail: every element that receives the virtualizer's
// measureElement ref MUST also carry a data-index attribute, so the
// virtualizer's indexFromElement/measureElement never warns
// "Missing attribute name 'data-index={index}' on measured element."
// (the pasted deployed-console warning; 04-root-cause.md RC3 — master source
// is correct, the deployed bundle likely predates e23659af/4c6cae73, so this
// check pins the correct behavior against regressions).
//
// Expected color at base (94c0b925): GREEN — SessionRail.tsx:1102-1108 sets
// data-index={virtualItem.index} on the same element that carries
// ref={sessionVirtualizer.measureElement}. If this test is RED at base, that
// is a real master bug discovery, not a check-authoring artifact.
//
// Mechanics: the REAL @tanstack/react-virtualizer would render 0 rows under
// jsdom (0-height scroll container), so the mock virtualizes ALL items (the
// established DocumentsPage.virtualization / issue-258 pattern) and its
// measureElement RECORDS every element React assigns to it via the ref
// callback — the assertion set is then exactly "every ref'd element carries
// data-index". Mocks for the shell store / dropdown-menu / api mirror
// SessionRail.adversarial.test.tsx.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";

import { SessionRail, _sessionCache } from "./SessionRail";
import type { ChatSession } from "@/lib/api";

// jsdom has no layout; the real virtualizer would render 0 rows. Virtualize
// ALL items (established repo pattern) and record every element React passes
// to measureElement so the data-index contract is assertable.
const { measuredElements } = vi.hoisted(() => ({
  measuredElements: [] as Element[],
}));

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(({ count }: { count: number }) => ({
    getVirtualItems: () =>
      Array.from({ length: count }, (_, i) => ({
        index: i,
        start: i * 64,
        size: 64,
        key: `v-${i}`,
      })),
    getTotalSize: () => count * 64,
    // Callback ref: React calls this with each mounted element (and null on
    // unmount). Records the exact elements the production ref would reach.
    measureElement: (el: Element | null) => {
      if (el) measuredElements.push(el);
    },
    scrollToIndex: vi.fn(),
    measure: vi.fn(),
  })),
}));

vi.mock("@/hooks/useDebounce", () => ({
  useDebounce: vi.fn((value: string) => [value, false]),
}));

vi.mock("@/lib/api", () => ({
  listChatSessions: vi.fn(),
  deleteChatSession: vi.fn(),
  updateChatSession: vi.fn(),
  getChatSession: vi.fn(),
}));

const mockSetSessionSearchQuery = vi.fn();

const createMockShellStore = (overrides = {}) => ({
  sessionRailOpen: true,
  rightPaneOpen: false,
  rightPaneWidth: 320,
  sessionRailWidth: 260,
  activeSessionId: null,
  activeSessionTitle: null,
  sessionListRefreshToken: 0,
  sessionSearchQuery: "",
  pinnedSessionIds: [],
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
  setSessionSearchQuery: mockSetSessionSearchQuery,
  togglePinSession: vi.fn(),
  isSessionPinned: vi.fn(),
  ...overrides,
});

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(),
}));

// Radix DropdownMenu cannot open in jsdom (SessionRail.adversarial pattern).
vi.mock("@/components/ui/dropdown-menu", async () => {
  const React = await import("react");
  return {
    DropdownMenu: ({ children }: { children: React.ReactNode }) =>
      React.createElement("div", null, children),
    DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) =>
      React.createElement("div", null, children),
    DropdownMenuContent: ({ children }: { children: React.ReactNode }) =>
      React.createElement("div", null, children),
    DropdownMenuItem: ({
      children,
      onClick,
      "aria-label": ariaLabel,
    }: {
      children: React.ReactNode;
      onClick?: () => void;
      "aria-label"?: string;
    }) =>
      React.createElement(
        "button",
        { type: "button", role: "menuitem", onClick, "aria-label": ariaLabel },
        children
      ),
    DropdownMenuSeparator: () => React.createElement("hr"),
  };
});

import * as api from "@/lib/api";
import * as useChatShellStoreModule from "@/stores/useChatShellStore";

function makeSession(id: number, title: string): ChatSession {
  return {
    id,
    title,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    vault_id: 1,
    message_count: 3,
  };
}

const sessions = [
  makeSession(1, "Alpha session"),
  makeSession(2, "Beta session"),
  makeSession(3, "Gamma session"),
];

const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <BrowserRouter>{children}</BrowserRouter>
);

describe("SessionRail virtualizer data-index guardrail (trace meridian-canvas-401-vault-zero AC4)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useChatShellStoreModule.useChatShellStore).mockReturnValue(
      createMockShellStore()
    );
    // @ts-expect-error - ResizeObserver not in jsdom
    global.ResizeObserver = class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
    vi.spyOn(console, "warn").mockImplementation(() => {});
    _sessionCache.data = null;
    _sessionCache.ts = 0;
    measuredElements.length = 0;
    vi.mocked(api.listChatSessions).mockResolvedValue({ sessions });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    _sessionCache.data = null;
    _sessionCache.ts = 0;
  });

  it("every element receiving the virtualizer measureElement ref carries a data-index attribute", async () => {
    render(
      <Wrapper>
        <SessionRail />
      </Wrapper>
    );

    await waitFor(() => {
      expect(screen.getByText("Alpha session")).toBeInTheDocument();
    });

    expect(
      measuredElements.length,
      "virtualizer measureElement received no elements — the virtualized branch did not render (mock contract broken, check is vacuous)"
    ).toBeGreaterThan(0);

    for (const el of measuredElements) {
      const dataIndex = el.getAttribute("data-index");
      expect(
        dataIndex !== null && /^\d+$/.test(dataIndex),
        `measured element <${el.tagName.toLowerCase()}> is missing a valid data-index attribute (virtualizer indexFromElement would warn: Missing attribute name 'data-index')`
      ).toBe(true);
    }

    const warnText = vi
      .mocked(console.warn)
      .mock.calls.map((args) => args.map((arg) => String(arg)).join(" "))
      .join("\n");
    expect(warnText, "no 'Missing attribute name' warning may be emitted during render").not.toContain(
      "Missing attribute name"
    );
  });
});
