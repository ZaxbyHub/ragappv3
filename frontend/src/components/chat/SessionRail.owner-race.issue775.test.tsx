import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { SessionRail, _sessionCache } from "@/components/chat/SessionRail";
import { useAuthStore } from "@/stores/useAuthStore";

const api = vi.hoisted(() => ({
  listChatSessions: vi.fn(),
  deleteChatSession: vi.fn(),
  updateChatSession: vi.fn(),
  getChatSession: vi.fn(),
}));
const fixture = vi.hoisted(() => ({
  chatShell: {} as Record<string, unknown>,
  vault: { id: 7, name: "Shared vault" },
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));
vi.mock("@/hooks/useDebounce", () => ({ useDebounce: (value: string) => [value, false] }));
vi.mock("@/fixtures/TestModeContext", () => ({ useTestMode: () => false }));
vi.mock("@/stores/useChatShellStore", () => ({ useChatShellStore: () => fixture.chatShell }));
vi.mock("@/stores/useVaultStore", () => ({ useVaultStore: () => ({ getActiveVault: () => fixture.vault }) }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function session(id: number, title: string, vaultId = 7) {
  return {
    id,
    title,
    vault_id: vaultId,
    created_at: "2026-10-06T00:00:00.000Z",
    updated_at: "2026-10-06T00:00:00.000Z",
    message_count: 0,
  };
}

describe("SessionRail ownership boundaries (issue #775)", () => {
  const originalStore = useAuthStore.getState();
  const originalCache = { data: _sessionCache.data, vaultId: _sessionCache.vaultId, ts: _sessionCache.ts };
  const setActiveSessionId = vi.fn();
  let resizeObserverDescriptor: PropertyDescriptor | undefined;

  beforeEach(() => {
    vi.resetAllMocks();
    _sessionCache.data = null;
    _sessionCache.vaultId = undefined;
    _sessionCache.ts = 0;
    fixture.vault = { id: 7, name: "Shared vault" };
    fixture.chatShell = {
      activeSessionId: null,
      sessionSearchQuery: "",
      pinnedSessionIds: [],
      setSessionSearchQuery: vi.fn(),
      togglePinSession: vi.fn(),
      isSessionPinned: vi.fn(() => false),
      setActiveSessionId,
      setActiveSessionTitle: vi.fn(),
      sessionListRefreshToken: 0,
    };
    resizeObserverDescriptor = Object.getOwnPropertyDescriptor(window, "ResizeObserver");
    Object.defineProperty(window, "ResizeObserver", {
      configurable: true,
      value: class { observe() {} unobserve() {} disconnect() {} },
    });
    useAuthStore.setState({
      ...originalStore,
      user: { id: 1, username: "A", full_name: "A User", role: "member" },
      accessToken: "token-a",
      isAuthenticated: true,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    useAuthStore.setState(originalStore, true);
    _sessionCache.data = originalCache.data;
    _sessionCache.vaultId = originalCache.vaultId;
    _sessionCache.ts = originalCache.ts;
    if (resizeObserverDescriptor) {
      Object.defineProperty(window, "ResizeObserver", resizeObserverDescriptor);
    } else {
      delete (window as { ResizeObserver?: unknown }).ResizeObserver;
    }
  });

  it("refreshes the same vault for B, activates B, and rejects A's late read", async () => {
    const aRead = deferred<{ sessions: ReturnType<typeof session>[] }>();
    const a = session(101, "A session");
    const b = session(202, "B session");
    api.listChatSessions.mockImplementationOnce(() => aRead.promise).mockResolvedValueOnce({ sessions: [b] });

    try {
      render(<MemoryRouter><SessionRail vaultId={7} /></MemoryRouter>);
      await waitFor(() => expect(api.listChatSessions).toHaveBeenCalledTimes(1));
      act(() => useAuthStore.setState({ user: { id: 2, username: "B", full_name: "B User", role: "member" }, accessToken: "token-b", isAuthenticated: true }));
      await waitFor(() => expect(api.listChatSessions).toHaveBeenCalledTimes(2));
      await screen.findByText("B session");
      fireEvent.click(screen.getByText("B session"));
      expect(setActiveSessionId).toHaveBeenCalledWith("202");
      await act(async () => { aRead.resolve({ sessions: [a] }); await Promise.resolve(); });
      expect(screen.getByText("B session")).toBeInTheDocument();
      expect(screen.queryByText("A session")).not.toBeInTheDocument();
      expect(_sessionCache.data?.map((item) => item.id)).toEqual([202]);
    } finally {
      await act(async () => { aRead.resolve({ sessions: [] }); await Promise.resolve(); });
    }
  });

  it("does not let A's real Radix rename mutation overwrite B in the same vault", async () => {
    const aRename = deferred<ReturnType<typeof session>>();
    const a = session(101, "A session");
    const b = session(202, "B session");
    api.listChatSessions.mockResolvedValueOnce({ sessions: [a] }).mockResolvedValueOnce({ sessions: [b] });
    api.updateChatSession.mockImplementationOnce(() => aRename.promise);

    try {
      render(<MemoryRouter><SessionRail vaultId={7} /></MemoryRouter>);
      await screen.findByText("A session");
      const opener = screen.getByLabelText("More options");
      fireEvent.pointerDown(opener, { button: 0, ctrlKey: false });
      fireEvent.click(opener);
      fireEvent.click(await screen.findByText("Rename"));
      const inputs = screen.getAllByRole("textbox");
      const renameInput = inputs[inputs.length - 1];
      fireEvent.change(renameInput, { target: { value: "A renamed" } });
      fireEvent.keyDown(renameInput, { key: "Enter" });
      await waitFor(() => expect(api.updateChatSession).toHaveBeenCalled());
      act(() => useAuthStore.setState({ user: { id: 2, username: "B", full_name: "B User", role: "member" }, accessToken: "token-b", isAuthenticated: true }));
      await screen.findByText("B session");
      await act(async () => { aRename.resolve({ ...a, title: "A renamed" }); await Promise.resolve(); });
      expect(screen.getByText("B session")).toBeInTheDocument();
      expect(screen.queryByText("A renamed")).not.toBeInTheDocument();
      expect(_sessionCache.data?.map((item) => item.id)).toEqual([202]);
    } finally {
      await act(async () => { aRename.resolve(a); await Promise.resolve(); });
    }
  });

  it("does not let a deferred read from vault A replace vault B", async () => {
    const aRead = deferred<{ sessions: ReturnType<typeof session>[] }>();
    const a = session(101, "A vault session", 7);
    const b = session(202, "B vault session", 8);
    api.listChatSessions.mockImplementationOnce(() => aRead.promise).mockResolvedValueOnce({ sessions: [b] });

    try {
      const view = render(<MemoryRouter><SessionRail vaultId={7} /></MemoryRouter>);
      await waitFor(() => expect(api.listChatSessions).toHaveBeenCalledWith(7));
      fixture.vault = { id: 8, name: "Vault B" };
      view.rerender(<MemoryRouter><SessionRail vaultId={8} /></MemoryRouter>);
      await screen.findByText("B vault session");
      await act(async () => { aRead.resolve({ sessions: [a] }); await Promise.resolve(); });
      expect(screen.getByText("B vault session")).toBeInTheDocument();
      expect(screen.queryByText("A vault session")).not.toBeInTheDocument();
      expect(_sessionCache.vaultId).toBe(8);
    } finally {
      await act(async () => { aRead.resolve({ sessions: [] }); await Promise.resolve(); });
    }
  });

  it("does not let an old-vault rename mutation publish after the vault changes", async () => {
    const aRename = deferred<ReturnType<typeof session>>();
    const a = session(101, "A vault session", 7);
    const b = session(202, "B vault session", 8);
    api.listChatSessions.mockResolvedValueOnce({ sessions: [a] }).mockResolvedValueOnce({ sessions: [b] });
    api.updateChatSession.mockImplementationOnce(() => aRename.promise);

    try {
      const view = render(<MemoryRouter><SessionRail vaultId={7} /></MemoryRouter>);
      await screen.findByText("A vault session");
      const opener = screen.getByLabelText("More options");
      fireEvent.pointerDown(opener, { button: 0, ctrlKey: false });
      fireEvent.click(opener);
      fireEvent.click(await screen.findByText("Rename"));
      const inputs = screen.getAllByRole("textbox");
      fireEvent.change(inputs[inputs.length - 1], { target: { value: "A renamed" } });
      fireEvent.keyDown(inputs[inputs.length - 1], { key: "Enter" });
      await waitFor(() => expect(api.updateChatSession).toHaveBeenCalled());
      fixture.vault = { id: 8, name: "Vault B" };
      view.rerender(<MemoryRouter><SessionRail vaultId={8} /></MemoryRouter>);
      await screen.findByText("B vault session");
      await act(async () => { aRename.resolve({ ...a, title: "A renamed" }); await Promise.resolve(); });
      expect(screen.getByText("B vault session")).toBeInTheDocument();
      expect(screen.queryByText("A renamed")).not.toBeInTheDocument();
      expect(_sessionCache.vaultId).toBe(8);
    } finally {
      await act(async () => { aRename.resolve(a); await Promise.resolve(); });
    }
  });

  it("does not repopulate the shared cache after an unmounted rail settles", async () => {
    const lateRead = deferred<{ sessions: ReturnType<typeof session>[] }>();
    api.listChatSessions.mockImplementationOnce(() => lateRead.promise);

    const view = render(<MemoryRouter><SessionRail vaultId={7} /></MemoryRouter>);
    try {
      await waitFor(() => expect(api.listChatSessions).toHaveBeenCalledWith(7));
      view.unmount();
      await act(async () => { lateRead.resolve({ sessions: [session(101, "A session")] }); await Promise.resolve(); });
      expect(_sessionCache.data).toBeNull();
      expect(_sessionCache.vaultId).toBeUndefined();
    } finally {
      await act(async () => { lateRead.resolve({ sessions: [] }); await Promise.resolve(); });
    }
  });
});
