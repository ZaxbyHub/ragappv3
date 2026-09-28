// frontend/src/stores/useChatShellStore.pinStorageSync.test.ts
// Issue #685 — the cross-tab half of the pinned-sessions fix: a `storage`
// event from another tab REPLACES the in-memory pinned list (no union, so a
// cross-tab UNPIN is honored), and removals/clears/unparseable writes are
// mirrored as "no pins" so this tab never resurrects pins storage no longer
// holds. Also covers F-005: a failed persist keeps memory equal to storage so
// a phantom in-memory pin can never block unpinning.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { useChatShellStore } from "./useChatShellStore";

const PINNED_SESSIONS_KEY = "ragapp_pinned_sessions";

interface StorageSham {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
  removeItem: (key: string) => void;
  clear: () => void;
  readonly length: number;
  key: (index: number) => string | null;
}

function installFunctionalLocalStorage(): StorageSham {
  const backing = new Map<string, string>();
  const sham: StorageSham = {
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
  };
  Object.defineProperty(window, "localStorage", {
    value: sham,
    writable: true,
    configurable: true,
  });
  return sham;
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

  it("removal of the key from another tab mirrors as no pins", () => {
    useChatShellStore.setState({ pinnedSessionIds: [1, 2] });
    window.localStorage.removeItem(PINNED_SESSIONS_KEY);

    window.dispatchEvent(
      new StorageEvent("storage", {
        key: PINNED_SESSIONS_KEY,
        newValue: null,
      })
    );

    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([]);
  });

  it("a cross-tab clear() mirrors as no pins", () => {
    useChatShellStore.setState({ pinnedSessionIds: [4] });

    window.dispatchEvent(new StorageEvent("storage", { key: null, newValue: null }));
    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([]);
  });

  it("hostile or unparseable newValues collapse to no pins, not stale state", () => {
    useChatShellStore.setState({ pinnedSessionIds: [4] });

    window.dispatchEvent(
      new StorageEvent("storage", { key: PINNED_SESSIONS_KEY, newValue: "null" })
    );
    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([]);

    window.dispatchEvent(
      new StorageEvent("storage", { key: PINNED_SESSIONS_KEY, newValue: "garbage" })
    );
    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([]);

    window.dispatchEvent(
      new StorageEvent("storage", {
        key: PINNED_SESSIONS_KEY,
        newValue: JSON.stringify([1, "two", 3]),
      })
    );
    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([1, 3]);
  });

  it("storage events for unrelated keys are ignored", () => {
    useChatShellStore.setState({ pinnedSessionIds: [4] });

    window.dispatchEvent(
      new StorageEvent("storage", { key: "unrelated_key", newValue: "1" })
    );
    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([4]);
  });

  it("toggle after a cross-tab change keeps the other tab's pins", () => {
    useChatShellStore.setState({ pinnedSessionIds: [1] });
    window.localStorage.setItem(PINNED_SESSIONS_KEY, JSON.stringify([1, 2]));

    useChatShellStore.getState().togglePinSession(3);

    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([1, 2, 3]);
    expect(window.localStorage.getItem(PINNED_SESSIONS_KEY)).toBe(JSON.stringify([1, 2, 3]));
  });

  it("a failed persist keeps memory equal to storage so the pin stays removable (F-005)", () => {
    const sham = installFunctionalLocalStorage();
    window.localStorage.setItem(PINNED_SESSIONS_KEY, JSON.stringify([]));

    const originalSetItem = sham.setItem;
    let failNextWrite = true;
    sham.setItem = (key: string, value: string) => {
      if (failNextWrite) {
        failNextWrite = false;
        throw new Error("quota exceeded");
      }
      originalSetItem(key, value);
    };

    // Pin write fails: memory reverts to storage truth (no phantom pin).
    useChatShellStore.getState().togglePinSession(9);
    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([]);

    // The next toggle writes through and pins normally.
    useChatShellStore.getState().togglePinSession(9);
    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([9]);
    expect(window.localStorage.getItem(PINNED_SESSIONS_KEY)).toBe(JSON.stringify([9]));
  });
});
