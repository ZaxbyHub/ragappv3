import { StrictMode, type InputHTMLAttributes, type ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import { SessionRail, _sessionCache } from "@/components/chat/SessionRail";
import type { ChatSession } from "@/lib/api";
import { getJwtAccessToken, setJwtAccessToken } from "@/lib/api";
import {
  captureAuthOwner,
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";
import { useAuthStore } from "@/stores/useAuthStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useChatStore } from "@/stores/useChatStore";
import { useVaultStore } from "@/stores/useVaultStore";

const network = vi.hoisted(() => ({
  listChatSessions: vi.fn(),
  updateChatSession: vi.fn(),
  deleteChatSession: vi.fn(),
  getChatSession: vi.fn(),
}));

const uiCapture = vi.hoisted(() => ({
  menuActions: new Map<string, Array<(event: unknown) => void>>(),
  renameKeyDowns: [] as Array<(event: unknown) => void>,
}));

const notifications = vi.hoisted(() => ({
  toast: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  listChatSessions: network.listChatSessions,
  updateChatSession: network.updateChatSession,
  deleteChatSession: network.deleteChatSession,
  getChatSession: network.getChatSession,
}));

vi.mock("sonner", () => ({
  toast: Object.assign(notifications.toast, { error: notifications.error }),
}));

// Radix's portal/focus machinery is not reliable in jsdom. Keep the actual
// SessionItem and its callback closures, while rendering the menu boundary as
// ordinary elements so the callbacks remain observable and invokable.
vi.mock("@/components/ui/dropdown-menu", async () => {
  const React = await import("react");
  return {
    DropdownMenu: ({ children }: { children: ReactNode }) =>
      React.createElement("div", null, children),
    DropdownMenuTrigger: ({ children }: { children: ReactNode; asChild?: boolean }) =>
      React.createElement(React.Fragment, null, children),
    DropdownMenuContent: ({ children }: { children: ReactNode }) =>
      React.createElement("div", { role: "menu" }, children),
    DropdownMenuSeparator: () => React.createElement("hr"),
    DropdownMenuItem: ({
      children,
      onClick,
      ...props
    }: {
      children: ReactNode;
      onClick?: (event: unknown) => void;
      "aria-label"?: string;
    }) => {
      const label = props["aria-label"];
      if (label && onClick) {
        const handlers = uiCapture.menuActions.get(label) ?? [];
        handlers.push(onClick);
        uiCapture.menuActions.set(label, handlers);
      }
      return React.createElement(
        "button",
        { type: "button", ...props, onClick },
        children,
      );
    },
  };
});

// Capture the actual edit handler while retaining a real input element and
// normal controlled-input behavior.
vi.mock("@/components/ui/input", async () => {
  const React = await import("react");
  const Input = React.forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
    (props, ref) => {
      if (props["aria-label"] === "Edit session title" && props.onKeyDown) {
        uiCapture.renameKeyDowns.push(props.onKeyDown as unknown as (event: unknown) => void);
      }
      return React.createElement("input", { ...props, ref });
    },
  );
  Input.displayName = "Input";
  return { Input };
});

type AuthUser = NonNullable<ReturnType<typeof useAuthStore.getState>["user"]>;
type VaultRecord = ReturnType<typeof useVaultStore.getState>["vaults"][number];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function session(id: number, title: string, vaultId = 7): ChatSession {
  return {
    id,
    title,
    vault_id: vaultId,
    created_at: "2026-10-06T00:00:00.000Z",
    updated_at: "2026-10-06T00:00:00.000Z",
    message_count: 0,
  };
}

function user(id: number, role: AuthUser["role"] = "member"): AuthUser {
  return {
    id,
    username: `user-${id}`,
    full_name: `User ${id}`,
    role,
    is_active: true,
  };
}

function vault(id: number): VaultRecord {
  return {
    id,
    name: `Vault ${id}`,
    description: null,
    file_count: 1,
    created_at: "2026-10-06T00:00:00.000Z",
    updated_at: "2026-10-06T00:00:00.000Z",
  } as VaultRecord;
}

function RoutePath() {
  return <output data-testid="route-path">{useLocation().pathname}</output>;
}

function renderRail(vaultId = 7, initialEntry = "/chat", strict = false) {
  const content = (
    <>
      <RoutePath />
      <SessionRail vaultId={vaultId} />
    </>
  );
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      {strict ? <StrictMode>{content}</StrictMode> : content}
    </MemoryRouter>,
  );
}

function lastMenuAction(label: string): ((event: unknown) => void) | undefined {
  return uiCapture.menuActions.get(label)?.at(-1);
}

function firstMenuAction(label: string): ((event: unknown) => void) | undefined {
  return uiCapture.menuActions.get(label)?.[0];
}

function invokeMenuAction(action: ((event: unknown) => void) | undefined) {
  action?.({ stopPropagation: vi.fn() });
}

function setRealUser(next: AuthUser, token: string) {
  useAuthStore.setState({
    user: next,
    accessToken: token,
    isAuthenticated: true,
    isInitialized: true,
  });
  publishAuthPrincipal(next);
  setJwtAccessToken(token);
}

function replaceRealUser(id: number, token: string) {
  reserveReplacementAuthOwner();
  setRealUser(user(id), token);
}

describe("SessionRail ownership, mutation, and cache boundaries (issue #775)", () => {
  const initialAuth = useAuthStore.getState();
  const initialChatShell = useChatShellStore.getState();
  const initialChat = useChatStore.getState();
  const initialVault = useVaultStore.getState();
  const initialJwt = getJwtAccessToken();
  const initialCache = { ..._sessionCache };
  let resizeObserverDescriptor: PropertyDescriptor | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    uiCapture.menuActions.clear();
    uiCapture.renameKeyDowns.length = 0;
    Object.assign(_sessionCache, {
      data: null,
      vaultId: undefined,
      owner: undefined,
      principalGeneration: undefined,
      readAttempt: undefined,
      ts: 0,
    });
    useChatShellStore.setState(
      {
        ...initialChatShell,
        activeSessionId: null,
        activeSessionTitle: null,
        sessionSearchQuery: "",
        pinnedSessionIds: [],
        sessionListRefreshToken: 0,
      },
      true,
    );
    useChatStore.setState(initialChat, true);
    useVaultStore.setState(
      {
        ...initialVault,
        vaults: [vault(7), vault(8)],
        activeVaultId: 7,
        loading: false,
        error: null,
      },
      true,
    );
    resizeObserverDescriptor = Object.getOwnPropertyDescriptor(window, "ResizeObserver");
    Object.defineProperty(window, "ResizeObserver", {
      configurable: true,
      value: class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    });
    reserveReplacementAuthOwner();
    setRealUser(user(1), "jwt-a");
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    reserveReplacementAuthOwner();
    useAuthStore.setState(initialAuth, true);
    publishAuthPrincipal(initialAuth.user ? { id: initialAuth.user.id, role: initialAuth.user.role } : null);
    setJwtAccessToken(initialJwt);
    useChatShellStore.setState(initialChatShell, true);
    useChatStore.setState(initialChat, true);
    useVaultStore.setState(initialVault, true);
    Object.assign(_sessionCache, initialCache);
    if (resizeObserverDescriptor) {
      Object.defineProperty(window, "ResizeObserver", resizeObserverDescriptor);
    } else {
      delete (window as { ResizeObserver?: unknown }).ResizeObserver;
    }
  });

  it("rejects a retained A delete callback while B's same-id list is held", async () => {
    const aRead = deferred<{ sessions: ChatSession[] }>();
    const bRead = deferred<{ sessions: ChatSession[] }>();
    network.listChatSessions.mockReturnValueOnce(aRead.promise).mockReturnValueOnce(bRead.promise);
    renderRail();

    try {
      await waitFor(() => expect(network.listChatSessions).toHaveBeenCalledTimes(1));
      await act(async () => {
        aRead.resolve({ sessions: [session(55, "A title")] });
        await Promise.resolve();
      });
      await screen.findByText("A title");
      const oldDelete = lastMenuAction("Delete session");

      act(() => replaceRealUser(2, "jwt-b"));
      await waitFor(() => expect(network.listChatSessions).toHaveBeenCalledTimes(2));
      const ownerB = captureAuthOwner();
      const activeBeforeOldCallback = useChatShellStore.getState().activeSessionId;
      act(() => invokeMenuAction(oldDelete));
      expect(network.deleteChatSession).not.toHaveBeenCalled();
      expect(useChatShellStore.getState().activeSessionId).toBe(activeBeforeOldCallback);
      expect(captureAuthOwner()).toBe(ownerB);
      expect(ownerB.signal.aborted).toBe(false);
      expect(getJwtAccessToken()).toBe("jwt-b");

      await act(async () => {
        bRead.resolve({ sessions: [session(55, "B title")] });
        await Promise.resolve();
      });
      await screen.findByText("B title");
      expect(screen.queryByText("A title")).not.toBeInTheDocument();
      expect(_sessionCache.data?.map((item) => item.title)).toEqual(["B title"]);
    } finally {
      await act(async () => {
        aRead.resolve({ sessions: [] });
        bRead.resolve({ sessions: [] });
        await Promise.resolve();
      });
    }
  });

  it("retires A's held list response before same-id B can publish", async () => {
    const aRead = deferred<{ sessions: ChatSession[] }>();
    network.listChatSessions.mockReturnValueOnce(aRead.promise).mockResolvedValueOnce({
      sessions: [session(55, "B title")],
    });
    renderRail();

    try {
      await waitFor(() => expect(network.listChatSessions).toHaveBeenCalledTimes(1));
      act(() => replaceRealUser(2, "jwt-b"));
      await screen.findByText("B title");
      await act(async () => {
        aRead.resolve({ sessions: [session(55, "A title")] });
        await Promise.resolve();
      });
      expect(screen.getByText("B title")).toBeInTheDocument();
      expect(screen.queryByText("A title")).not.toBeInTheDocument();
      expect(_sessionCache.data?.map((item) => item.title)).toEqual(["B title"]);
    } finally {
      await act(async () => {
        aRead.resolve({ sessions: [] });
        await Promise.resolve();
      });
    }
  });

  it("retires a held A→B→A vault list across the ABA return", async () => {
    const firstA = deferred<{ sessions: ChatSession[] }>();
    const bRead = deferred<{ sessions: ChatSession[] }>();
    const secondA = deferred<{ sessions: ChatSession[] }>();
    network.listChatSessions
      .mockReturnValueOnce(firstA.promise)
      .mockReturnValueOnce(bRead.promise)
      .mockReturnValueOnce(secondA.promise);
    const view = renderRail(7);

    try {
      await waitFor(() => expect(network.listChatSessions).toHaveBeenCalledWith(7));
      view.rerender(
        <MemoryRouter initialEntries={["/chat"]}>
          <RoutePath />
          <SessionRail vaultId={8} />
        </MemoryRouter>,
      );
      view.rerender(
        <MemoryRouter initialEntries={["/chat"]}>
          <RoutePath />
          <SessionRail vaultId={7} />
        </MemoryRouter>,
      );
      await waitFor(() => expect(network.listChatSessions).toHaveBeenCalledTimes(3));
      await act(async () => {
        bRead.resolve({ sessions: [session(8, "retired B", 8)] });
        secondA.resolve({ sessions: [session(7, "fresh A", 7)] });
        await Promise.resolve();
      });
      await screen.findByText("fresh A");
      await act(async () => {
        firstA.resolve({ sessions: [session(1, "stale A", 7)] });
        await Promise.resolve();
      });
      expect(screen.getByText("fresh A")).toBeInTheDocument();
      expect(screen.queryByText("stale A")).not.toBeInTheDocument();
      expect(screen.queryByText("retired B")).not.toBeInTheDocument();
      expect(_sessionCache.vaultId).toBe(7);
      expect(_sessionCache.data?.map((item) => item.title)).toEqual(["fresh A"]);
    } finally {
      await act(async () => {
        firstA.resolve({ sessions: [] });
        bRead.resolve({ sessions: [] });
        secondA.resolve({ sessions: [] });
        await Promise.resolve();
      });
    }
  });

  it("admits one rename when the same live callback is invoked twice", async () => {
    const heldUpdate = deferred<void>();
    network.listChatSessions.mockResolvedValue({ sessions: [session(55, "original")] });
    network.updateChatSession.mockReturnValue(heldUpdate.promise);
    renderRail();

    try {
      await screen.findByText("original");
      fireEvent.click(screen.getByLabelText("Rename session"));
      const input = screen.getByLabelText("Edit session title");
      fireEvent.change(input, { target: { value: "renamed once" } });
      const keyDown = uiCapture.renameKeyDowns.at(-1);
      expect(keyDown).toBeDefined();
      act(() => {
        const event = { key: "Enter", preventDefault: vi.fn() };
        keyDown?.(event);
        keyDown?.(event);
      });
      await waitFor(() => expect(network.updateChatSession).toHaveBeenCalledTimes(1));
      expect(await screen.findByText("renamed once")).toBeInTheDocument();
      heldUpdate.resolve();
      await act(async () => {
        await heldUpdate.promise;
      });
      expect(network.updateChatSession).toHaveBeenCalledTimes(1);
    } finally {
      heldUpdate.resolve();
      await act(async () => {
        await heldUpdate.promise;
      });
    }
  });

  it("keeps one delete Undo timer and never calls the API after Undo", async () => {
    network.listChatSessions.mockResolvedValue({ sessions: [session(55, "delete once")] });
    renderRail();

    await screen.findByText("delete once");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const oldDelete = lastMenuAction("Delete session");
    act(() => {
      invokeMenuAction(oldDelete);
      invokeMenuAction(oldDelete);
    });
    expect(network.deleteChatSession).not.toHaveBeenCalled();
    expect(notifications.toast).toHaveBeenCalledTimes(1);
    const [, toastOptions] = notifications.toast.mock.calls[0] as [
      string,
      { action: { onClick: () => void } },
    ];
    await act(async () => { toastOptions.action.onClick(); await Promise.resolve(); });
    expect(screen.getByText("delete once")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(network.deleteChatSession).not.toHaveBeenCalled();
    expect(notifications.toast).toHaveBeenCalledTimes(1);
  });

  it("reverts a rejected rename and admits the next positive rename", async () => {
    network.listChatSessions.mockResolvedValue({ sessions: [session(55, "original")] });
    network.updateChatSession
      .mockRejectedValueOnce(new Error("rename rejected"))
      .mockResolvedValueOnce(undefined);
    renderRail();

    await screen.findByText("original");
    fireEvent.click(screen.getByLabelText("Rename session"));
    fireEvent.change(screen.getByLabelText("Edit session title"), {
      target: { value: "rejected title" },
    });
    fireEvent.keyDown(screen.getByLabelText("Edit session title"), { key: "Enter" });
    await waitFor(() => expect(network.updateChatSession).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("original")).toBeInTheDocument());
    expect(notifications.error).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByLabelText("Rename session"));
    fireEvent.change(screen.getByLabelText("Edit session title"), {
      target: { value: "accepted title" },
    });
    fireEvent.keyDown(screen.getByLabelText("Edit session title"), { key: "Enter" });
    await waitFor(() => expect(network.updateChatSession).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("accepted title")).toBeInTheDocument();
  });

  it("restores a failed active delete without stealing B's selection or route", async () => {
    useChatShellStore.setState({ activeSessionId: "1", activeSessionTitle: "A active" });
    network.listChatSessions.mockResolvedValue({
      sessions: [session(1, "A active"), session(2, "B selected")],
    });
    network.deleteChatSession.mockRejectedValue(new Error("delete rejected"));
    renderRail();

    await screen.findByText("A active");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    fireEvent.click(within(screen.getByRole("button", { name: "Chat session: A active" })).getByRole("button", { name: "Delete session" }));
    expect(useChatShellStore.getState().activeSessionId).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Chat session: B selected" }));
    expect(useChatShellStore.getState().activeSessionId).toBe("2");
    expect(screen.getByTestId("route-path")).toHaveTextContent("/chat/2");

    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(network.deleteChatSession).toHaveBeenCalledTimes(1);
    expect(screen.getByText("A active")).toBeInTheDocument();
    expect(screen.getByText("B selected")).toBeInTheDocument();
    expect(useChatShellStore.getState().activeSessionId).toBe("2");
    expect(screen.getByTestId("route-path")).toHaveTextContent("/chat/2");
  });

  it("restores Undo after selecting B without stealing B's active route", async () => {
    useChatShellStore.setState({ activeSessionId: "1", activeSessionTitle: "A active" });
    network.listChatSessions.mockResolvedValue({
      sessions: [session(1, "A active"), session(2, "B selected")],
    });
    renderRail();

    await screen.findByText("A active");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    fireEvent.click(within(screen.getByRole("button", { name: "Chat session: A active" })).getByRole("button", { name: "Delete session" }));
    const [, toastOptions] = notifications.toast.mock.calls[0] as [
      string,
      { action: { onClick: () => void } },
    ];
    fireEvent.click(screen.getByRole("button", { name: "Chat session: B selected" }));
    expect(useChatShellStore.getState().activeSessionId).toBe("2");
    expect(screen.getByTestId("route-path")).toHaveTextContent("/chat/2");
    await act(async () => { toastOptions.action.onClick(); await Promise.resolve(); });
    expect(screen.getByText("A active")).toBeInTheDocument();
    expect(useChatShellStore.getState().activeSessionId).toBe("2");
    expect(screen.getByTestId("route-path")).toHaveTextContent("/chat/2");
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(network.deleteChatSession).not.toHaveBeenCalled();
  });

  it("shows a malformed-list error, retries, and publishes the valid list", async () => {
    network.listChatSessions
      .mockResolvedValueOnce({ sessions: "malformed" })
      .mockResolvedValueOnce({ sessions: [session(55, "retry success")] });
    renderRail();

    await screen.findByText("Failed to load sessions");
    expect(network.listChatSessions).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("retry success");
    expect(network.listChatSessions).toHaveBeenCalledTimes(2);
    expect(_sessionCache.data?.map((item) => item.title)).toEqual(["retry success"]);
  });

  it("does not trust an ownerless seeded cache for the real owner", async () => {
    Object.assign(_sessionCache, {
      data: [session(991, "unowned stale")],
      vaultId: 7,
      owner: undefined,
      principalGeneration: undefined,
      ts: Date.now(),
    });
    network.listChatSessions.mockResolvedValue({ sessions: [session(55, "fresh network")] });
    renderRail();

    await screen.findByText("fresh network");
    expect(network.listChatSessions).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("unowned stale")).not.toBeInTheDocument();
  });

  it("keeps a fresh cache when an older cross-instance physical read settles later", async () => {
    const oldRead = deferred<{ sessions: ChatSession[] }>();
    network.listChatSessions
      .mockReturnValueOnce(oldRead.promise)
      .mockResolvedValueOnce({ sessions: [session(2, "fresh cache")] });
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <SessionRail vaultId={7} />
        <SessionRail vaultId={7} />
      </MemoryRouter>,
    );

    try {
      await waitFor(() => expect(network.listChatSessions).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(_sessionCache.data?.map((item) => item.title)).toEqual(["fresh cache"]));
      await act(async () => {
        oldRead.resolve({ sessions: [session(1, "older physical")] });
        await Promise.resolve();
      });
      expect(_sessionCache.data?.map((item) => item.title)).toEqual(["fresh cache"]);
      expect(_sessionCache.readAttempt).toBe(2);
    } finally {
      await act(async () => {
        oldRead.resolve({ sessions: [] });
        await Promise.resolve();
      });
    }
  });

  it("retires a queued A retry before the fresh B read can publish", async () => {
    const retryA = deferred<{ sessions: ChatSession[] }>();
    network.listChatSessions
      .mockResolvedValueOnce({ sessions: "malformed" })
      .mockReturnValueOnce(retryA.promise)
      .mockResolvedValueOnce({ sessions: [session(2, "B fresh")] });
    renderRail();

    try {
      await screen.findByText("Failed to load sessions");
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await waitFor(() => expect(network.listChatSessions).toHaveBeenCalledTimes(2));
      act(() => replaceRealUser(2, "jwt-b"));
      await screen.findByText("B fresh");
      await act(async () => {
        retryA.resolve({ sessions: [session(1, "A stale retry")] });
        await Promise.resolve();
      });
      expect(screen.queryByText("A stale retry")).not.toBeInTheDocument();
      expect(_sessionCache.data?.map((item) => item.title)).toEqual(["B fresh"]);
    } finally {
      await act(async () => {
        retryA.resolve({ sessions: [] });
        await Promise.resolve();
      });
    }
  });

  it("retires a held StrictMode rename after unmount", async () => {
    const heldUpdate = deferred<void>();
    network.listChatSessions.mockResolvedValue({ sessions: [session(55, "before unmount")] });
    network.updateChatSession.mockReturnValue(heldUpdate.promise);
    const view = renderRail(7, "/chat", true);

    try {
      await screen.findByText("before unmount");
      fireEvent.click(screen.getByLabelText("Rename session"));
      fireEvent.change(screen.getByLabelText("Edit session title"), {
        target: { value: "late rename" },
      });
      fireEvent.keyDown(screen.getByLabelText("Edit session title"), { key: "Enter" });
      await waitFor(() => expect(network.updateChatSession).toHaveBeenCalledTimes(1));
      view.unmount();
      heldUpdate.resolve();
      await act(async () => {
        await heldUpdate.promise;
      });
      expect(notifications.error).not.toHaveBeenCalled();
    } finally {
      heldUpdate.resolve();
      await act(async () => {
        await heldUpdate.promise;
      });
    }
  });

  it("completes a confirmed delete after benign unmount", async () => {
    network.deleteChatSession.mockResolvedValue(undefined);
    network.listChatSessions.mockResolvedValue({ sessions: [session(55, "timer owned")] });
    const view = renderRail();

    await screen.findByText("timer owned");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    act(() => invokeMenuAction(lastMenuAction("Delete session")));
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(network.deleteChatSession).toHaveBeenCalledTimes(1);
    expect(network.deleteChatSession).toHaveBeenCalledWith(55);
    expect(notifications.error).not.toHaveBeenCalled();
  });
});
