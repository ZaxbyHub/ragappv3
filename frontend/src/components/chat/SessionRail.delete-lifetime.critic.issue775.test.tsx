import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionRail } from "@/components/chat/SessionRail";

const sessionFixture = vi.hoisted(() => ({
  session: {
    id: 775,
    title: "Retained session",
    created_at: "2026-10-08T12:00:00.000Z",
    updated_at: "2026-10-08T12:00:00.000Z",
    message_count: 1,
    is_pinned: false,
  },
}));

const chatFixture = vi.hoisted(() => ({
  newChat: vi.fn(),
}));

const apiFixture = vi.hoisted(() => ({
  deleteChatSession: vi.fn().mockResolvedValue(undefined),
  listChatSessions: vi.fn().mockResolvedValue([]),
  updateChatSession: vi.fn().mockResolvedValue(undefined),
  getChatSession: vi.fn().mockResolvedValue({ data: sessionFixture.session }),
}));

const toastFixture = vi.hoisted(() => {
  const toast: any = vi.fn();
  toast.error = vi.fn();
  return { toast };
});

const vaultFixture = vi.hoisted(() => {
  let activeVault: { id: number } | null = { id: 1 };
  const listeners = new Set<() => void>();
  let state = { getActiveVault: () => activeVault };
  return {
    getActiveVault: () => activeVault,
    getSnapshot: () => state,
    setActiveVault: (next: { id: number } | null) => {
      activeVault = next;
      state = { getActiveVault: () => activeVault };
      listeners.forEach((listener) => listener());
    },
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    reset: () => {
      activeVault = { id: 1 };
      state = { getActiveVault: () => activeVault };
      listeners.clear();
    },
  };
});

const shellFixture = vi.hoisted(() => {
  const state = {
    activeSessionId: null as string | null,
    activeSessionTitle: null as string | null,
    setActiveSession: vi.fn(),
    setActiveSessionId: vi.fn(),
    setActiveSessionTitle: vi.fn(),
    clearActiveSession: vi.fn(),
    sessionSearchQuery: "",
    setSessionSearchQuery: vi.fn(),
    togglePinSession: vi.fn(),
    pinnedSessionIds: [] as number[],
    setPinnedSessionIds: vi.fn(),
    sessionListRefreshToken: 0,
    isSessionPinned: (sessionId: number) => state.pinnedSessionIds.includes(sessionId),
  };
  return state;
});

vi.mock("@/fixtures/chat", () => ({
  mockChatSessions: [sessionFixture.session],
}));

vi.mock("@/fixtures/TestModeContext", () => ({
  useTestMode: () => true,
}));

vi.mock("@/lib/api", async () => {
  return {
    deleteChatSession: apiFixture.deleteChatSession,
    listChatSessions: apiFixture.listChatSessions,
    updateChatSession: apiFixture.updateChatSession,
    getChatSession: apiFixture.getChatSession,
  };
});

vi.mock("@/stores/useVaultStore", async () => {
  const { useSyncExternalStore } = await vi.importActual<typeof import("react")>("react");
  const useVaultStore = Object.assign(
    (selector?: (state: { getActiveVault: typeof vaultFixture.getActiveVault }) => unknown) => {
      const state = useSyncExternalStore(
        vaultFixture.subscribe,
        vaultFixture.getSnapshot,
        vaultFixture.getSnapshot,
      );
      return selector ? selector(state) : state;
    },
    { getState: () => vaultFixture.getSnapshot() },
  );
  return { useVaultStore };
});

vi.mock("@/stores/useChatShellStore", () => {
  const useChatShellStore = Object.assign(
    (selector?: (state: typeof shellFixture) => unknown) =>
      selector ? selector(shellFixture) : shellFixture,
    {
    getState: () => shellFixture,
    },
  );
  return { useChatShellStore };
});

vi.mock("@/stores/useChatStore", () => ({
  useChatStore: Object.assign(
    (selector?: (state: Record<string, never>) => unknown) =>
      selector ? selector({}) : {},
    { getState: () => chatFixture },
  ),
}));

vi.mock("@/lib/commandPaletteActions", () => ({
  useCommandPaletteAction: () => undefined,
}));

vi.mock("@/components/ui/dropdown-menu", () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuSeparator: () => null,
  DropdownMenuItem: ({
    children,
    onClick,
    ...props
  }: {
    children: React.ReactNode;
    onClick?: () => void;
    [key: string]: unknown;
  }) => (
    <button type="button" onClick={onClick} {...props}>
      {children}
    </button>
  ),
}));

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 64,
    getVirtualItems: () =>
      Array.from({ length: count }, (_, index) => ({
        index,
        start: index * 64,
        size: 64,
        key: index,
      })),
    measureElement: vi.fn(),
    scrollToIndex: vi.fn(),
  }),
}));

vi.mock("sonner", () => ({ toast: toastFixture.toast }));

function rail(vaultId?: number) {
  return (
    <MemoryRouter>
      <SessionRail {...(vaultId === undefined ? {} : { vaultId })} />
    </MemoryRouter>
  );
}

async function renderRail(vaultId?: number) {
  const view = render(rail(vaultId));
  await waitFor(() => expect(screen.getByText(sessionFixture.session.title)).toBeVisible());
  return view;
}

function clickDelete() {
  fireEvent.click(screen.getByRole("button", { name: "More options" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete session" }));
  expect(toastFixture.toast).toHaveBeenCalled();
}

function latestUndo() {
  const calls = toastFixture.toast.mock.calls;
  const [, options] = calls[calls.length - 1] ?? [];
  return options?.action?.onClick as (() => void) | undefined;
}

describe("SessionRail confirmed delete lifetime", () => {
  beforeEach(() => {
    apiFixture.deleteChatSession.mockReset().mockResolvedValue(undefined);
    apiFixture.listChatSessions.mockReset().mockResolvedValue([]);
    apiFixture.updateChatSession.mockReset().mockResolvedValue(undefined);
    apiFixture.getChatSession.mockReset().mockResolvedValue({ data: sessionFixture.session });
    chatFixture.newChat.mockReset();
    toastFixture.toast.mockReset();
    toastFixture.toast.error.mockReset();
    shellFixture.activeSessionId = null;
    shellFixture.activeSessionTitle = null;
    shellFixture.sessionSearchQuery = "";
    shellFixture.pinnedSessionIds = [];
    shellFixture.sessionListRefreshToken = 0;
    vaultFixture.reset();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("finishes an unscoped confirmed delete after benign rail unmount", async () => {
    const view = await renderRail();
    vi.useFakeTimers();
    await clickDelete();

    view.unmount();
    await act(async () => {
      vi.advanceTimersByTime(5001);
      await Promise.resolve();
    });

    expect(apiFixture.deleteChatSession).toHaveBeenCalledTimes(1);
    expect(apiFixture.deleteChatSession).toHaveBeenCalledWith(sessionFixture.session.id);
  });

  it("keeps an unscoped confirmed delete across selected-vault changes", async () => {
    await renderRail();
    vi.useFakeTimers();
    await clickDelete();

    act(() => vaultFixture.setActiveVault({ id: 2 }));
    await act(async () => {
      vi.advanceTimersByTime(5001);
      await Promise.resolve();
    });

    expect(apiFixture.deleteChatSession).toHaveBeenCalledTimes(1);
  });

  it("lets Undo cancel a finite pending delete after benign unmount without stale UI publication", async () => {
    const view = await renderRail();
    vi.useFakeTimers();
    await clickDelete();
    const undo = latestUndo();
    expect(undo).toBeTypeOf("function");

    view.unmount();
    await act(async () => {
      undo?.();
      vi.advanceTimersByTime(5001);
      await Promise.resolve();
    });

    expect(apiFixture.deleteChatSession).not.toHaveBeenCalled();
    expect(toastFixture.toast.error).not.toHaveBeenCalled();
  });

  it("restores the removed row when mounted Undo cancels the pending delete", async () => {
    await renderRail();
    vi.useFakeTimers();
    await clickDelete();
    const undo = latestUndo();
    expect(screen.queryByText(sessionFixture.session.title)).not.toBeInTheDocument();

    await act(async () => {
      undo?.();
      await Promise.resolve();
    });

    expect(apiFixture.deleteChatSession).not.toHaveBeenCalled();
    expect(screen.getByText(sessionFixture.session.title)).toBeVisible();
  });

  it("cancels a scoped delete when the committed wrapper scope changes A to B to A", async () => {
    const view = await renderRail(1);
    vi.useFakeTimers();
    await clickDelete();

    view.rerender(rail(2));
    view.rerender(rail(1));
    await act(async () => {
      vi.advanceTimersByTime(5001);
      await Promise.resolve();
    });

    expect(apiFixture.deleteChatSession).not.toHaveBeenCalled();
  });

  it("admits a new delete after an obsolete scoped pending record is replaced", async () => {
    const view = await renderRail(1);
    vi.useFakeTimers();
    await clickDelete();

    await act(async () => {
      view.rerender(rail(2));
      await Promise.resolve();
    });
    expect(screen.getByText(sessionFixture.session.title)).toBeVisible();

    await clickDelete();
    await act(async () => {
      vi.advanceTimersByTime(5001);
      await Promise.resolve();
    });

    expect(apiFixture.deleteChatSession).toHaveBeenCalledTimes(1);
    expect(apiFixture.deleteChatSession).toHaveBeenCalledWith(sessionFixture.session.id);
  });

  it("cancels a pending delete when the authenticated owner is replaced", async () => {
    const view = await renderRail();
    vi.useFakeTimers();
    await clickDelete();

    const { reserveReplacementAuthOwner } = await import("@/lib/api/auth-lifecycle");
    act(() => {
      reserveReplacementAuthOwner();
    });
    await act(async () => {
      vi.advanceTimersByTime(5001);
      await Promise.resolve();
    });
    view.unmount();

    expect(apiFixture.deleteChatSession).not.toHaveBeenCalled();
  });

  it('shows a confirmed delete failure after benign unmount while the auth context remains current', async () => {
    const view = await renderRail();
    vi.useFakeTimers();
    let rejectDelete!: (error: Error) => void;
    apiFixture.deleteChatSession.mockImplementationOnce(
      () => new Promise((_, reject) => { rejectDelete = reject; }),
    );
    await clickDelete();
    view.unmount();

    await act(async () => {
      vi.advanceTimersByTime(5001);
      await Promise.resolve();
    });
    expect(apiFixture.deleteChatSession).toHaveBeenCalledTimes(1);
    await act(async () => { rejectDelete(new Error('server rejected delete')); });

    expect(toastFixture.toast.error).toHaveBeenCalledWith(
      'Could not delete session.',
      { description: 'server rejected delete' },
    );
  });

  it('suppresses a late delete failure after the authenticated owner is replaced', async () => {
    await renderRail();
    vi.useFakeTimers();
    let rejectDelete!: (error: Error) => void;
    apiFixture.deleteChatSession.mockImplementationOnce(
      () => new Promise((_, reject) => { rejectDelete = reject; }),
    );
    await clickDelete();

    await act(async () => {
      vi.advanceTimersByTime(5001);
      await Promise.resolve();
    });
    expect(apiFixture.deleteChatSession).toHaveBeenCalledTimes(1);
    const { reserveReplacementAuthOwner } = await import('@/lib/api/auth-lifecycle');
    act(() => { reserveReplacementAuthOwner(); });
    await act(async () => { rejectDelete(new Error('late failure')); });

    expect(toastFixture.toast.error).not.toHaveBeenCalled();
  });

  it('keeps the global delete failure toast when benign unmount happens during local rollback', async () => {
    shellFixture.activeSessionId = "775";
    const view = await renderRail();
    vi.useFakeTimers();
    shellFixture.setActiveSessionId
      .mockImplementationOnce((next: string | null) => { shellFixture.activeSessionId = next; })
      .mockImplementationOnce((next: string | null) => {
        shellFixture.activeSessionId = next;
        if (next === "775") view.unmount();
      });
    let rejectDelete!: (error: Error) => void;
    apiFixture.deleteChatSession.mockImplementationOnce(
      () => new Promise((_, reject) => { rejectDelete = reject; }),
    );
    await clickDelete();

    await act(async () => {
      vi.advanceTimersByTime(5001);
      await Promise.resolve();
    });
    expect(apiFixture.deleteChatSession).toHaveBeenCalledTimes(1);
    await act(async () => { rejectDelete(new Error('server rejected delete')); });

    expect(toastFixture.toast.error).toHaveBeenCalledWith(
      'Could not delete session.',
      { description: 'server rejected delete' },
    );
  });
});
