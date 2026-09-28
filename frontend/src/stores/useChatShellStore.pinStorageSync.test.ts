// frontend/src/stores/useChatShellStore.pinStorageSync.test.ts
// Issue #685 — the cross-tab half of the pinned-sessions fix: a `storage`
// event from another tab REPLACES the in-memory pinned list (no union, so a
// cross-tab UNPIN is honored), and a cross-tab clear event is ignored by the
// key filter until the next toggle re-reads storage.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { useChatShellStore } from "./useChatShellStore";

const PINNED_SESSIONS_KEY = "ragapp_pinned_sessions";

function installFunctionalLocalStorage(): void {
  const backing = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    value: {
      getItem: (key: string) => backing.get(key) ?? null,
      setItem: (key: string, value: string) => {
        backing.set(key, value);
      },
      removeItem: (key: string) => {
        backing.delete(key);
      },
      clear: () => {
        backing.clear();
      },
      get length() {
        return backing.size;
      },
      key: (index: number) => Array.from(backing.keys())[index] ?? null,
    },
    writable: true,
    configurable: true,
  });
}

describe("useChatShellStore pinned-session cross-tab sync (issue #685)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installFunctionalLocalStorage();
    useChatShellStore.setState({ pinnedSessionIds: [] });
  });

  it("storage event replaces the pinned list from the other tab", () => {
    useChatShellStore.setState({ pinnedSessionIds: [1] });
    window.localStorage.setItem(PINNED_SESSIONS_KEY, JSON.stringify([2, 3]));

    window.dispatchEvent(
      new StorageEvent("storage", {
        key: PINNED_SESSIONS_KEY,
        newValue: JSON.stringify([2, 3]),
      })
    );

    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([2, 3]);
  });

  it("cross-tab unpin is honored, not unioned back", () => {
    useChatShellStore.setState({ pinnedSessionIds: [1, 2] });
    window.localStorage.setItem(PINNED_SESSIONS_KEY, JSON.stringify([2]));

    window.dispatchEvent(
      new StorageEvent("storage", {
        key: PINNED_SESSIONS_KEY,
        newValue: JSON.stringify([2]),
      })
    );

    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([2]);
  });

  it("storage events for other keys and cross-tab clears are ignored", () => {
    useChatShellStore.setState({ pinnedSessionIds: [4] });

    window.dispatchEvent(
      new StorageEvent("storage", { key: "unrelated_key", newValue: "1" })
    );
    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([4]);

    // A cross-tab clear() surfaces as key === null / newValue === null; the
    // listener leaves the in-memory list alone (the next toggle re-reads).
    window.dispatchEvent(new StorageEvent("storage", { key: null, newValue: null }));
    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([4]);
  });

  it("toggle after a cross-tab change keeps the other tab's pins", () => {
    useChatShellStore.setState({ pinnedSessionIds: [1] });
    window.localStorage.setItem(PINNED_SESSIONS_KEY, JSON.stringify([1, 2]));

    useChatShellStore.getState().togglePinSession(3);

    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([1, 2, 3]);
    expect(window.localStorage.getItem(PINNED_SESSIONS_KEY)).toBe(JSON.stringify([1, 2, 3]));
  });
});
