// Issue #823 composer-draft boundary (unfrozen, plan-critic rounds 2-3 plus
// the swarm-pr-review/hermes feedback rounds):
//
// 1. user A's unsent draft must not survive an identity switch through EITHER
//    delayed writer — the pending debounce timer's trailing flush AND the
//    unmount flush. The identity reset bumps the draft generation; every
//    delayed write scheduled under the old generation must drop instead of
//    re-creating the cleared localStorage key.
// 2. A removal that the storage surface REFUSES must not leak the previous
//    principal's text to the next one: the key namespace is principal-scoped,
//    so the incoming principal reads its own (absent) key even though the old
//    key survived the wipe (hermes review at the final head, HIGH).
// 3. A burst that straddles the reset must still persist: the trailing-flush
//    skip decision is generation-aware, so the next principal's identical
//    text is written rather than treated as already-persisted (out-of-band
//    review R4 + PRR-007).
//
// Real modules under test: Composer.tsx, @/lib/chatDrafts, useChatStore.
// Peripheral stores mocked per the Composer.draft.test.tsx harness.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

const mockAuthState = vi.hoisted(() => ({ userId: 11 as number | null }));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector?: (s: unknown) => unknown) => {
    const state = { user: mockAuthState.userId === null ? null : { id: mockAuthState.userId } };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));
vi.mock("@/stores/useChatModeStore", () => ({
  useChatModeStore: vi.fn((selector?: (s: unknown) => unknown) => {
    const state = { chatMode: "thinking", setChatMode: vi.fn() };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));
vi.mock("@/stores/useLlmHealthStore", () => ({
  useLlmHealthStore: vi.fn((selector?: (s: unknown) => unknown) => {
    const state = { thinking: true, instant: true, refresh: vi.fn() };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));
vi.mock("@/stores/useSettingsStore", () => ({
  useSettingsStore: vi.fn((selector?: (s: unknown) => unknown) => {
    const state = { settings: { default_chat_mode: "thinking" }, formData: { default_chat_mode: "thinking" } };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));
vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: Object.assign(
    vi.fn((selector?: (s: unknown) => unknown) => {
      const state = {
        activeVaultId: 1,
        vaults: [{ id: 1, name: "Test Vault", file_count: 1 }],
        getActiveVault: () => ({ id: 1, name: "Test Vault", file_count: 1 }),
      };
      return typeof selector === "function" ? selector(state) : state;
    }),
    { getState: () => ({ activeVaultId: 1 }) }
  ),
}));
vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/api",
  attachCsrfInterceptor: vi.fn(),
  uploadDocument: vi.fn(),
  getDocumentStatus: vi.fn(),
}));

import { Composer } from "./Composer";
import { useChatStore } from "@/stores/useChatStore";
import {
  clearAllChatDrafts,
  DRAFT_PREFIX,
  getDraftKey,
  invalidatePendingDraftWrites,
} from "@/lib/chatDrafts";

const PRINCIPAL_A = 11;

function installLocalStorage(): Map<string, string> {
  const store = new Map<string, string>();
  const impl = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(window, "localStorage", { value: impl, configurable: true });
  return store;
}

/** The composer input is addressed by its accessible label (repo idiom). */
function mainTextarea(): HTMLTextAreaElement {
  return screen.getByLabelText("Message input") as HTMLTextAreaElement;
}

describe("Composer draft writers die at the identity boundary (issue #823)", () => {
  let storage: Map<string, string>;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    mockAuthState.userId = PRINCIPAL_A;
    useChatStore.getState().newChat();
    storage = installLocalStorage();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("the pending debounce trailing flush does not resurrect a cleared draft", () => {
    const { unmount } = render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = mainTextarea();

    act(() => {
      fireEvent.change(textarea, { target: { value: "user A's unsent draft" } });
    });
    // Leading write landed synchronously; the trailing flush is scheduled.
    expect(storage.get(getDraftKey(null, PRINCIPAL_A))).toBe("user A's unsent draft");

    // Identity switch: bump the generation, clear the input, clear the keys
    // (the exact sequence useAuthStore.resetChatStateForIdentitySwitch runs).
    invalidatePendingDraftWrites();
    useChatStore.getState().newChat();
    useChatStore.getState().setInput("");
    clearAllChatDrafts();
    expect(storage.get(getDraftKey(null, PRINCIPAL_A))).toBeUndefined();

    // The trailing edge fires after the reset: it must DROP, not re-create.
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(storage.has(DRAFT_PREFIX + PRINCIPAL_A + ":new")).toBe(false);
    expect(Array.from(storage.keys()).filter((k) => k.startsWith(DRAFT_PREFIX))).toEqual([]);
    unmount();
  });

  it("the unmount flush does not resurrect a cleared draft", () => {
    const { unmount } = render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = mainTextarea();
    act(() => {
      fireEvent.change(textarea, { target: { value: "user A's last keystrokes" } });
    });
    expect(storage.get(getDraftKey(null, PRINCIPAL_A))).toBe("user A's last keystrokes");

    invalidatePendingDraftWrites();
    useChatStore.getState().newChat();
    useChatStore.getState().setInput("");
    clearAllChatDrafts();

    act(() => {
      unmount(); // unmount flush runs synchronously in the cleanup effect
    });
    expect(Array.from(storage.keys()).filter((k) => k.startsWith(DRAFT_PREFIX))).toEqual([]);
  });

  it("a burst straddling the reset still persists the next principal's text", () => {
    // Out-of-band review R4 + PRR-007: a single keystroke used to be swallowed
    // by the lastWrittenRef equality, so the generation gate was unpinned. A
    // burst whose trailing edge fires after the switch must be written.
    const { unmount, rerender } = render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = mainTextarea();

    act(() => {
      fireEvent.change(textarea, { target: { value: "shared burst text" } });
    });
    expect(storage.get(getDraftKey(null, PRINCIPAL_A))).toBe("shared burst text");

    // Identity switch mid-debounce (the timer is still pending).
    invalidatePendingDraftWrites();
    useChatStore.getState().newChat();
    useChatStore.getState().setInput("");
    clearAllChatDrafts();

    // The next principal types the SAME text before the stale edge fires.
    mockAuthState.userId = 22;
    rerender(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    act(() => {
      fireEvent.change(mainTextarea(), { target: { value: "shared burst text" } });
    });
    act(() => {
      vi.advanceTimersByTime(2000);
    });

    // The stale edge dropped A's text; the new principal's write landed.
    expect(storage.get(getDraftKey(null, 22))).toBe("shared burst text");
    expect(storage.get(getDraftKey(null, PRINCIPAL_A))).toBeUndefined();
    unmount();
  });

  it("a removal the storage refuses cannot leak the previous principal's draft", () => {
    // Hermes review (final head, HIGH): clearAllChatDrafts is best-effort, so
    // a key that survives removal must still be unreadable by the next
    // principal. The principal-scoped namespace is that boundary.
    const { unmount } = render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = mainTextarea();
    act(() => {
      fireEvent.change(textarea, { target: { value: "user A private draft" } });
    });
    const keyA = getDraftKey(null, PRINCIPAL_A);
    expect(storage.get(keyA)).toBe("user A private draft");

    // The storage surface refuses to remove A's key.
    const realRemove = storage.delete.bind(storage);
    storage.delete = (k: string) => {
      if (k === keyA) throw new Error("QuotaExceededError");
      realRemove(k);
    };

    invalidatePendingDraftWrites();
    useChatStore.getState().newChat();
    useChatStore.getState().setInput("");
    const failures = clearAllChatDrafts();
    expect(failures).toBe(1); // the residual is reported, not silent
    expect(storage.get(keyA)).toBe("user A private draft"); // the key survived

    // User B mounts the composer: their namespace is empty, so A's text is
    // never loaded into the input.
    mockAuthState.userId = 22;
    unmount();
    const second = render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    expect(useChatStore.getState().input).toBe("");
    expect(second.getByLabelText("Message input")).toBeDefined();
    second.unmount();
  });
});
