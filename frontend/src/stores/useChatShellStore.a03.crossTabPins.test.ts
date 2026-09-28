// frontend/src/stores/useChatShellStore.a03.crossTabPins.test.ts
// Issue #685 — persisted pin mirrors must survive cross-tab writes. Another
// tab persisting ragapp_pinned_sessions and firing the storage event has to
// be honored by THIS tab's next local toggle: the persisted set is the union
// of the cross-tab write and the local toggle, not the stale in-memory list.
// At the pre-fix tree the store ignores the storage event entirely and
// togglePinSession persists from its stale in-memory pinnedSessionIds,
// silently dropping the other tab's pin.

import { beforeEach, describe, expect, it } from "vitest";
import { useChatShellStore } from "./useChatShellStore";

const PINNED_SESSIONS_KEY = "ragapp_pinned_sessions";

// setup.ts replaces localStorage with a silent no-op; this check needs real
// storage semantics, so install a functional in-memory localStorage backed by
// a Map (fresh per test — no state leaks between tests).
function installFunctionalLocalStorage(): void {
  const backing = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    value: {
      getItem: (key: string) => backing.get(key) ?? null,
      setItem: (key: string, value: string) => {
        backing.set(key, String(value));
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

describe("useChatShellStore cross-tab pinned sessions (issue #685)", () => {
  beforeEach(() => {
    installFunctionalLocalStorage();
    useChatShellStore.setState({ pinnedSessionIds: [] });
  });

  it("cross-tab pin changes survive a local toggle", () => {
    // This tab currently holds a single pin in memory...
    useChatShellStore.setState({ pinnedSessionIds: [1] });
    // ...while another tab persists a superset and fires the storage event.
    localStorage.setItem(PINNED_SESSIONS_KEY, "[1,2]");
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: PINNED_SESSIONS_KEY,
        newValue: "[1,2]",
      })
    );

    // A local toggle in THIS tab must build on the cross-tab state, not the
    // stale in-memory list.
    useChatShellStore.getState().togglePinSession(3);

    const persisted = localStorage.getItem(PINNED_SESSIONS_KEY);
    const parsed = JSON.parse(persisted ?? "[]") as number[];
    expect(parsed.slice().sort((a, b) => a - b).join(",")).toBe("1,2,3");
  });
});
