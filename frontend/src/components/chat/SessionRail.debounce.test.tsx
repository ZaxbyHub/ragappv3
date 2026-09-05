import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, rerender } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { SessionRail } from "./SessionRail";
import * as api from "@/lib/api";
import * as useChatShellStoreModule from "@/stores/useChatShellStore";

// Mock the API module
vi.mock("@/lib/api", () => ({
  listChatSessions: vi.fn(),
  deleteChatSession: vi.fn(),
  updateChatSession: vi.fn(),
  getChatSession: vi.fn(),
}));

// Mock the store - mutable state that simulates Zustand behavior
let storeState = {
  sessionRailOpen: true,
  rightPaneOpen: false,
  rightPaneWidth: 320,
  activeSessionId: null as string | null,
  sessionSearchQuery: "",
  pinnedSessionIds: [] as number[],
  toggleSessionRail: vi.fn(),
  toggleRightPane: vi.fn(),
  setRightPaneWidth: vi.fn(),
  setActiveSessionId: vi.fn(),
  openSessionRail: vi.fn(),
  closeSessionRail: vi.fn(),
  openRightPane: vi.fn(),
  closeRightPane: vi.fn(),
  setSessionSearchQuery: vi.fn((query: string) => {
    storeState.sessionSearchQuery = query;
  }),
  togglePinSession: vi.fn(),
  isSessionPinned: vi.fn((id: number) => storeState.pinnedSessionIds.includes(id)),
};

// Track subscribers for reactivity simulation
let subscribers: Array<() => void> = [];

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(() => {
    // Return store state - calling the mock returns the current state
    return storeState;
  }),
}));

// Mock localStorage
const localStorageMock = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
};
Object.defineProperty(window, "localStorage", {
  value: localStorageMock,
});

// Wrapper component with Router
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <BrowserRouter>{children}</BrowserRouter>
);

describe("SessionRail filteredSessions debounce behavior", () => {
  const mockSessions = [
    {
      id: 1,
      vault_id: 1,
      title: "Alpha Session",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      message_count: 5,
    },
    {
      id: 2,
      vault_id: 1,
      title: "Beta Conversation",
      created_at: new Date(Date.now() - 86400000).toISOString(),
      updated_at: new Date(Date.now() - 86400000).toISOString(),
      message_count: 3,
    },
    {
      id: 3,
      vault_id: 1,
      title: "Gamma Chat",
      created_at: new Date(Date.now() - 7 * 86400000).toISOString(),
      updated_at: new Date(Date.now() - 7 * 86400000).toISOString(),
      message_count: 0,
    },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    // Reset store state
    storeState = {
      sessionRailOpen: true,
      rightPaneOpen: false,
      rightPaneWidth: 320,
      activeSessionId: null,
      sessionSearchQuery: "",
      pinnedSessionIds: [],
      toggleSessionRail: vi.fn(),
      toggleRightPane: vi.fn(),
      setRightPaneWidth: vi.fn(),
      setActiveSessionId: vi.fn(),
      openSessionRail: vi.fn(),
      closeSessionRail: vi.fn(),
      openRightPane: vi.fn(),
      closeRightPane: vi.fn(),
      setSessionSearchQuery: vi.fn((query: string) => {
        storeState.sessionSearchQuery = query;
      }),
      togglePinSession: vi.fn(),
      isSessionPinned: vi.fn((id: number) => storeState.pinnedSessionIds.includes(id)),
    };
    subscribers = [];
    vi.useFakeTimers();
    vi.mocked(api.listChatSessions).mockResolvedValue({ sessions: mockSessions });
    vi.mocked(api.getChatSession).mockResolvedValue({ id: 1, messages: [] });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("returns all sessions immediately when debouncedSearchQuery is empty string", async () => {
    render(
      <Wrapper>
        <SessionRail />
      </Wrapper>
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.getByText("Gamma Chat")).toBeInTheDocument();
  });

  it("does NOT recompute filteredSessions immediately when sessionSearchQuery changes", async () => {
    const { rerender: r } = render(
      <Wrapper>
        <SessionRail />
      </Wrapper>
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.getByText("Gamma Chat")).toBeInTheDocument();

    // Update store state and trigger re-render
    storeState.sessionSearchQuery = "beta";
    await act(async () => {
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    // Before debounce completes (300ms), ALL sessions should still be visible
    // because filteredSessions depends on debouncedSearchQuery, NOT sessionSearchQuery
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.getByText("Gamma Chat")).toBeInTheDocument();
  });

  it("recomputes filteredSessions after debounce delay (300ms) - partial advance", async () => {
    const { rerender: r } = render(
      <Wrapper>
        <SessionRail />
      </Wrapper>
    );

    await act(async () => {
      await Promise.resolve();
    });

    // Update store state
    storeState.sessionSearchQuery = "gamma";

    // Before debounce - all sessions visible
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.getByText("Gamma Chat")).toBeInTheDocument();

    // Rerender with new state but don't advance timers yet
    await act(async () => {
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    // Advance only 150ms - still before debounce completes (300ms needed)
    await act(async () => {
      vi.advanceTimersByTime(150);
    });

    // All sessions should still be visible (debounce not yet complete)
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.getByText("Gamma Chat")).toBeInTheDocument();

    // Advance remaining 150ms to complete the 300ms debounce
    await act(async () => {
      vi.advanceTimersByTime(150);
    });

    // After full debounce delay, only matching session should appear
    expect(screen.queryByText("Alpha Session")).not.toBeInTheDocument();
    expect(screen.queryByText("Beta Conversation")).not.toBeInTheDocument();
    expect(screen.getByText("Gamma Chat")).toBeInTheDocument();
  });

  it("filters sessions after debounce completes when query matches title", async () => {
    const { rerender: r } = render(
      <Wrapper>
        <SessionRail />
      </Wrapper>
    );

    await act(async () => {
      await Promise.resolve();
    });

    // Update store state
    storeState.sessionSearchQuery = "alpha";

    // Before debounce - all sessions visible
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.getByText("Gamma Chat")).toBeInTheDocument();

    // Rerender with new state
    await act(async () => {
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    // Advance time to trigger debounce
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    // After debounce - should filter to only matching sessions
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.queryByText("Beta Conversation")).not.toBeInTheDocument();
    expect(screen.queryByText("Gamma Chat")).not.toBeInTheDocument();
  });

  it("search matching is case-insensitive after debounce", async () => {
    const { rerender: r } = render(
      <Wrapper>
        <SessionRail />
      </Wrapper>
    );

    await act(async () => {
      await Promise.resolve();
    });

    // Use uppercase search query
    storeState.sessionSearchQuery = "ALPHA";

    // Rerender with new state
    await act(async () => {
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    // Before debounce - all sessions visible
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();

    // Advance time to trigger debounce
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    // Case-insensitive match should work - Alpha matches ALPHA
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.queryByText("Beta Conversation")).not.toBeInTheDocument();
  });

  it("clears filter when search query becomes empty after debounce", async () => {
    const { rerender: r } = render(
      <Wrapper>
        <SessionRail />
      </Wrapper>
    );

    await act(async () => {
      await Promise.resolve();
    });

    // Set search query
    storeState.sessionSearchQuery = "alpha";

    await act(async () => {
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    // Advance debounce
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    // Only Alpha should be visible
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.queryByText("Beta Conversation")).not.toBeInTheDocument();

    // Clear search
    storeState.sessionSearchQuery = "";

    await act(async () => {
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    // Still filtered before debounce completes
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();

    // Advance debounce for the clear
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    // After debounce, all sessions should be visible again
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.getByText("Gamma Chat")).toBeInTheDocument();
  });

  it("handles rapid search query changes correctly", async () => {
    const { rerender: r } = render(
      <Wrapper>
        <SessionRail />
      </Wrapper>
    );

    await act(async () => {
      await Promise.resolve();
    });

    // Rapid changes to search query
    storeState.sessionSearchQuery = "a";
    await act(async () => {
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    await act(async () => {
      vi.advanceTimersByTime(50);
      storeState.sessionSearchQuery = "al";
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    await act(async () => {
      vi.advanceTimersByTime(50);
      storeState.sessionSearchQuery = "alp";
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    await act(async () => {
      vi.advanceTimersByTime(50);
      storeState.sessionSearchQuery = "alph";
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    // Before final debounce completes, all sessions still visible
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.getByText("Gamma Chat")).toBeInTheDocument();

    // Wait for final debounce to complete
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    // Final debounced value "alph" - should filter sessions
    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.queryByText("Beta Conversation")).not.toBeInTheDocument();
    expect(screen.queryByText("Gamma Chat")).not.toBeInTheDocument();
  });

  it("repeats a first-message-content search correctly without remounting (UI-043)", async () => {
    // Search-by-first-message requires the session details fetch. Only
    // session 2's first message contains the needle — and no TITLE contains
    // it, so the match can only come from the fetched details.
    vi.mocked(api.getChatSession).mockImplementation(async (id: number) => ({
      id,
      vault_id: 1,
      title: null,
      created_at: "2026-05-01T00:00:00Z",
      updated_at: "2026-05-01T00:00:00Z",
      messages: [
        {
          id: id * 10,
          role: "user",
          content: id === 2 ? "where did I leave the needle" : "an unrelated opening question",
          sources: null,
          created_at: "2026-05-01T00:00:00Z",
        },
      ],
    }));

    const { rerender: r } = render(
      <Wrapper>
        <SessionRail />
      </Wrapper>
    );

    await act(async () => {
      await Promise.resolve();
    });

    // --- First search by first-message content ---
    storeState.sessionSearchQuery = "needle";
    await act(async () => {
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });

    // Advance the debounce and flush the details fetch microtasks.
    await act(async () => {
      vi.advanceTimersByTime(300);
      await Promise.resolve();
      await Promise.resolve();
    });

    // Details were fetched once per session; Beta matches via its first message.
    expect(api.getChatSession).toHaveBeenCalledTimes(3);
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.queryByText("Alpha Session")).not.toBeInTheDocument();
    expect(screen.queryByText("Gamma Chat")).not.toBeInTheDocument();

    // --- Clear the query ---
    storeState.sessionSearchQuery = "";
    await act(async () => {
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    expect(screen.getByText("Alpha Session")).toBeInTheDocument();
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.getByText("Gamma Chat")).toBeInTheDocument();

    // --- Search the SAME text again WITHOUT remounting ---
    storeState.sessionSearchQuery = "needle";
    await act(async () => {
      r(
        <Wrapper>
          <SessionRail />
        </Wrapper>
      );
    });
    await act(async () => {
      vi.advanceTimersByTime(300);
      await Promise.resolve();
      await Promise.resolve();
    });

    // With the old bug — clearing the details map on an empty query while
    // fetchedIdsRef kept its markers — this second search found nothing.
    expect(screen.getByText("Beta Conversation")).toBeInTheDocument();
    expect(screen.queryByText("Alpha Session")).not.toBeInTheDocument();
    expect(screen.queryByText("Gamma Chat")).not.toBeInTheDocument();

    // Details are retained: getChatSession is NOT called a second time for
    // any session (still 3 total, exactly one of them for session 2).
    expect(api.getChatSession).toHaveBeenCalledTimes(3);
    const callsForSessionTwo = api.getChatSession.mock.calls.filter(
      (call) => call[0] === 2
    );
    expect(callsForSessionTwo).toHaveLength(1);
  });
});
