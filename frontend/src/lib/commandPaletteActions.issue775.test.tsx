import { StrictMode, type PropsWithChildren } from "react";
import { act, cleanup, renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getCommandPaletteActionSnapshot,
  invokeCommandPaletteAction,
  type CommandPaletteActionGuard,
  type CommandPaletteActionRegistration,
  useCommandPaletteAction,
} from "@/lib/commandPaletteActions";
import { captureAuthOwner, reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";
import { useAuthStore } from "@/stores/useAuthStore";
import { useVaultStore } from "@/stores/useVaultStore";

function Router({ children }: PropsWithChildren) {
  return <MemoryRouter initialEntries={["/chat"]}>{children}</MemoryRouter>;
}

function StrictRouter({ children }: PropsWithChildren) {
  return (
    <StrictMode>
      <Router>{children}</Router>
    </StrictMode>
  );
}

function newChatRegistration(
  execute: CommandPaletteActionRegistration["execute"] = vi.fn(
    (_guard: CommandPaletteActionGuard) => undefined,
  ),
): CommandPaletteActionRegistration {
  return { id: "new-chat", label: "New chat", enabled: true, execute };
}

function snapshotForNewChat() {
  const snapshot = getCommandPaletteActionSnapshot().find(
    (entry) => entry.id === "new-chat",
  );
  if (!snapshot) throw new Error("New chat action is not registered");
  return snapshot;
}

afterEach(() => {
  cleanup();
  useAuthStore.setState({ user: null });
  useVaultStore.setState({ activeVaultId: null });
});

describe("command palette action ownership", () => {
  it("restores the previous duplicate receiver when the newest one unmounts", () => {
    const registration = newChatRegistration();
    const first = renderHook(() => useCommandPaletteAction(registration), {
      wrapper: Router,
    });
    const firstSnapshot = snapshotForNewChat();

    const second = renderHook(() => useCommandPaletteAction(registration), {
      wrapper: Router,
    });
    const secondSnapshot = snapshotForNewChat();

    expect(secondSnapshot.registration).not.toBe(firstSnapshot.registration);
    expect(getCommandPaletteActionSnapshot()).toContainEqual(secondSnapshot);

    second.unmount();
    expect(snapshotForNewChat().registration).toBe(firstSnapshot.registration);

    first.unmount();
    expect(
      getCommandPaletteActionSnapshot().some((entry) => entry.id === "new-chat"),
    ).toBe(false);
  });

  it("rejects an offered action after its authentication owner is replaced", () => {
    const execute = vi.fn((_guard: CommandPaletteActionGuard) => undefined);
    const registration = newChatRegistration(execute);
    const hook = renderHook(() => useCommandPaletteAction(registration), {
      wrapper: Router,
    });
    const oldGuard = hook.result.current;
    const offered = snapshotForNewChat();

    act(() => {
      reserveReplacementAuthOwner();
    });

    expect(oldGuard.isCurrent()).toBe(false);
    expect(invokeCommandPaletteAction(offered, oldGuard)).toBe(false);
    expect(execute).not.toHaveBeenCalled();
  });

  it("rejects a retained action immediately when the principal changes", () => {
    const execute = vi.fn((_guard: CommandPaletteActionGuard) => undefined);
    const registration = newChatRegistration(execute);
    const hook = renderHook(() => useCommandPaletteAction(registration), {
      wrapper: Router,
    });
    const oldGuard = hook.result.current;
    const offered = snapshotForNewChat();

    act(() => {
      useAuthStore.setState({
        user: {
          id: 9001,
          username: "replacement",
          full_name: "Replacement User",
          role: "member",
          is_active: true,
        },
      });
      expect(oldGuard.isCurrent()).toBe(false);
      expect(invokeCommandPaletteAction(offered, oldGuard)).toBe(false);
    });

    expect(execute).not.toHaveBeenCalled();
  });

  it("invalidates a retained guard across principal A to B to A in one act", () => {
    const principalA = {
      id: 1101,
      username: "principal-a",
      full_name: "Principal A",
      role: "member" as const,
      is_active: true,
    };
    const principalB = {
      id: 1102,
      username: "principal-b",
      full_name: "Principal B",
      role: "member" as const,
      is_active: true,
    };
    act(() => {
      useAuthStore.setState({ user: principalA });
    });
    const execute = vi.fn((_guard: CommandPaletteActionGuard) => undefined);
    const registration = newChatRegistration(execute);
    const hook = renderHook(() => useCommandPaletteAction(registration), {
      wrapper: Router,
    });
    const oldGuard = hook.result.current;
    const offered = snapshotForNewChat();

    act(() => {
      useAuthStore.setState({ user: principalB });
      useAuthStore.setState({ user: principalA });
      expect(useAuthStore.getState().user?.id).toBe(principalA.id);
      expect(oldGuard.isCurrent()).toBe(false);
      expect(invokeCommandPaletteAction(offered, oldGuard)).toBe(false);
    });

    expect(execute).not.toHaveBeenCalled();
  });

  it("rejects a retained action immediately when the same principal changes role", () => {
    const principalA = {
      id: 1201,
      username: "role-a",
      full_name: "Role A",
      role: "member" as const,
      is_active: true,
    };
    act(() => {
      useAuthStore.setState({ user: principalA });
    });
    const execute = vi.fn((_guard: CommandPaletteActionGuard) => undefined);
    const registration = newChatRegistration(execute);
    const hook = renderHook(() => useCommandPaletteAction(registration), {
      wrapper: Router,
    });
    const oldGuard = hook.result.current;
    const offered = snapshotForNewChat();

    act(() => {
      useAuthStore.setState({
        user: { ...principalA, role: "admin" as const },
      });
      expect(useAuthStore.getState().user?.role).toBe("admin");
      expect(oldGuard.isCurrent()).toBe(false);
      expect(invokeCommandPaletteAction(offered, oldGuard)).toBe(false);
    });

    expect(execute).not.toHaveBeenCalled();
  });

  it("preserves a guard and auth owner for same-principal profile updates", () => {
    const principalA = {
      id: 1301,
      username: "profile-a",
      full_name: "Profile A",
      role: "member" as const,
      is_active: true,
    };
    act(() => {
      useAuthStore.setState({ user: principalA });
    });
    const initialOwner = captureAuthOwner();
    const execute = vi.fn((_guard: CommandPaletteActionGuard) => undefined);
    const registration = newChatRegistration(execute);
    const hook = renderHook(() => useCommandPaletteAction(registration), {
      wrapper: Router,
    });
    const oldGuard = hook.result.current;
    const offered = snapshotForNewChat();

    act(() => {
      useAuthStore.setState({
        user: {
          ...principalA,
          username: "profile-renamed",
          full_name: "Profile Renamed",
        },
      });
      expect(oldGuard.isCurrent()).toBe(true);
      expect(captureAuthOwner()).toBe(initialOwner);
    });

    expect(invokeCommandPaletteAction(offered, oldGuard)).toBe(true);
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("invalidates a guard across a vault A to B to A replacement", () => {
    useVaultStore.setState({ activeVaultId: 101 });
    const execute = vi.fn((_guard: CommandPaletteActionGuard) => undefined);
    const registration = newChatRegistration(execute);
    const hook = renderHook(() => useCommandPaletteAction(registration), {
      wrapper: Router,
    });
    const oldGuard = hook.result.current;
    const offered = snapshotForNewChat();

    act(() => {
      useVaultStore.getState().setActiveVault(202);
      useVaultStore.getState().setActiveVault(101);
    });

    expect(useVaultStore.getState().activeVaultId).toBe(101);
    expect(oldGuard.isCurrent()).toBe(false);
    expect(invokeCommandPaletteAction(offered, oldGuard)).toBe(false);
    expect(execute).not.toHaveBeenCalled();
  });

  it("keeps StrictMode cleanup from leaving a duplicate registry entry", () => {
    const hook = renderHook(
      () => useCommandPaletteAction(newChatRegistration()),
      { wrapper: StrictRouter },
    );

    expect(
      getCommandPaletteActionSnapshot().filter((entry) => entry.id === "new-chat"),
    ).toHaveLength(1);

    hook.unmount();
    expect(
      getCommandPaletteActionSnapshot().some((entry) => entry.id === "new-chat"),
    ).toBe(false);
  });

  it("releases the running mutex when an async execution rejects", async () => {
    let rejectPending!: (reason: unknown) => void;
    const pending = new Promise<void>((_resolve, reject) => {
      rejectPending = reject;
    });
    const execute = vi
      .fn((_guard: CommandPaletteActionGuard): void | Promise<void> => pending)
      .mockImplementationOnce(() => pending)
      .mockImplementationOnce(() => undefined);
    const registration = newChatRegistration(execute);
    const hook = renderHook(() => useCommandPaletteAction(registration), {
      wrapper: Router,
    });
    const guard = hook.result.current;
    const offered = snapshotForNewChat();

    expect(invokeCommandPaletteAction(offered, guard)).toBe(true);
    expect(invokeCommandPaletteAction(offered, guard)).toBe(false);

    await act(async () => {
      rejectPending(new Error("expected execution rejection"));
      await expect(pending).rejects.toThrow("expected execution rejection");
      await Promise.resolve();
    });

    expect(invokeCommandPaletteAction(offered, guard)).toBe(true);
    expect(execute).toHaveBeenCalledTimes(2);
  });
});
