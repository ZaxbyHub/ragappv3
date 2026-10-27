// Issue #823 composer-draft boundary (unfrozen, plan-critic rounds 2-3):
// user A's unsent draft must not survive an identity switch through EITHER
// delayed writer — the pending debounce timer's trailing flush AND the
// unmount flush. The identity reset bumps the draft generation; every
// delayed write scheduled under the old generation must drop instead of
// re-creating the cleared localStorage key.
//
// Real modules under test: Composer.tsx, @/lib/chatDrafts, useChatStore.
// Peripheral stores mocked per the Composer.draft.test.tsx harness.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

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
    expect(storage.get(getDraftKey(null))).toBe("user A's unsent draft");

    // Identity switch: bump the generation, clear the input, clear the keys
    // (the exact sequence useAuthStore.resetChatStateForIdentitySwitch runs).
    invalidatePendingDraftWrites();
    useChatStore.getState().newChat();
    useChatStore.getState().setInput("");
    clearAllChatDrafts();
    expect(storage.get(getDraftKey(null))).toBeUndefined();

    // The trailing edge fires after the reset: it must DROP, not re-create.
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(storage.has(DRAFT_PREFIX + "new")).toBe(false);
    expect(Array.from(storage.keys()).filter((k) => k.startsWith(DRAFT_PREFIX))).toEqual([]);
    unmount();
  });

  it("the unmount flush does not resurrect a cleared draft", () => {
    const { unmount } = render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = mainTextarea();
    act(() => {
      fireEvent.change(textarea, { target: { value: "user A's last keystrokes" } });
    });
    expect(storage.get(getDraftKey(null))).toBe("user A's last keystrokes");

    invalidatePendingDraftWrites();
    useChatStore.getState().newChat();
    useChatStore.getState().setInput("");
    clearAllChatDrafts();

    act(() => {
      unmount(); // unmount flush runs synchronously in the cleanup effect
    });
    expect(Array.from(storage.keys()).filter((k) => k.startsWith(DRAFT_PREFIX))).toEqual([]);
  });
});

