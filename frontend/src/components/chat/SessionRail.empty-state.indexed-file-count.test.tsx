// MS-API-001 (#1153 AC4 parity): the session-rail empty state must gate on
// indexed_file_count, not the legacy total file_count. A vault with only
// pending/failed uploads (file_count > 0, indexed_file_count === 0) should
// still prompt "Upload documents", never "Start a new chat to begin".

import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useSyncExternalStore } from "react";
import { SessionRail } from "@/components/chat/SessionRail";

const vaultFixture = vi.hoisted(() => {
  let activeVault: Record<string, unknown> | null = { id: 1 };
  const listeners = new Set<() => void>();
  const state = { getActiveVault: () => activeVault };
  return {
    getActiveVault: () => activeVault,
    getSnapshot: () => state,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setVault: (next: Record<string, unknown> | null) => {
      activeVault = next;
      listeners.forEach((l) => l());
    },
    reset: () => {
      activeVault = { id: 1 };
      listeners.clear();
    },
  };
});

vi.mock("@/fixtures/chat", () => ({ mockChatSessions: [] }));

vi.mock("@/fixtures/TestModeContext", () => ({ useTestMode: () => true }));

vi.mock("@/lib/api", () => ({
  listChatSessions: vi.fn().mockResolvedValue({ sessions: [] }),
}));

vi.mock("@/stores/useVaultStore", () => {
  const useVaultStore = Object.assign(
    (selector?: (state: { getActiveVault: () => unknown }) => unknown) => {
      const s = useSyncExternalStore(
        vaultFixture.subscribe,
        vaultFixture.getSnapshot,
        vaultFixture.getSnapshot,
      );
      return selector ? selector(s) : s;
    },
    { getState: () => vaultFixture.getSnapshot() },
  );
  return { useVaultStore };
});

vi.mock("@/stores/useChatShellStore", () => {
  const state = {
    activeSessionId: null as string | null,
    sessionSearchQuery: "",
    setSessionSearchQuery: vi.fn(),
    pinnedSessionIds: [] as number[],
    sessionListRefreshToken: 0,
    isSessionPinned: () => false,
  };
  const useChatShellStore = Object.assign(
    (selector?: (s: typeof state) => unknown) => selector?.(state) ?? state,
    { getState: () => state },
  );
  return { useChatShellStore };
});

vi.mock("@/stores/useChatStore", () => ({
  useChatStore: Object.assign(() => ({}), { getState: () => ({}) }),
}));

vi.mock("@/lib/commandPaletteActions", () => ({ useCommandPaletteAction: () => undefined }));

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 64,
    getVirtualItems: () => [],
    measureElement: vi.fn(),
    scrollToIndex: vi.fn(),
  }),
}));

afterEach(() => vaultFixture.reset());

function renderRail() {
  return render(
    <MemoryRouter>
      <SessionRail vaultId={1} />
    </MemoryRouter>,
  );
}

describe("SessionRail empty state — indexed_file_count gating (MS-API-001)", () => {
  it("prompts to upload when the vault has files but none indexed", async () => {
    vaultFixture.setVault({ id: 1, file_count: 5, indexed_file_count: 0 });
    renderRail();
    await waitFor(() =>
      expect(
        screen.getByText(/Upload documents first so the assistant has something to ground/),
      ).toBeInTheDocument(),
    );
    // The legacy file_count > 0 must NOT surface the "start chatting" copy.
    expect(screen.queryByText("Start a new chat to begin a conversation")).not.toBeInTheDocument();
  });

  it("offers to start a chat when the vault has indexed documents", async () => {
    vaultFixture.setVault({ id: 1, file_count: 5, indexed_file_count: 3 });
    renderRail();
    await waitFor(() =>
      expect(screen.getByText("Start a new chat to begin a conversation")).toBeInTheDocument(),
    );
  });
});
