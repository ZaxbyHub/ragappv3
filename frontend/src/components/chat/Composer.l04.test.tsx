// frontend/src/components/chat/Composer.l04.test.tsx
// L04 / AC8 (frozen acceptance check) — the composer toolbar's slash and
// attach buttons must be keyboard reachable.
//
// At master both buttons hardcode tabIndex={-1} (Composer.tsx, the
// "Open slash commands" and "Attach file" buttons), removing them from the
// tab order: a keyboard user can reach neither the slash-command menu nor
// the attachment flow.
//
// Expected RED at master: "expected -1 to be 0".
//
// Harness: the minimal mock set from Composer.slash-button.test.tsx /
// Composer.draft.test.tsx (stores, api, dropzone, sonner; localStorage
// in-memory Map mock layered over the global vi.fn stubs from
// src/test/setup.ts).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { Composer } from "./Composer";

const mockChatState = vi.hoisted(() => ({
  input: "",
  inputError: null as string | null,
  activeChatId: null as string | null,
  setInput: vi.fn((value: string) => {
    mockChatState.input = value;
  }),
}));

vi.mock("@/stores/useChatStore", () => ({
  useChatStore: vi.fn((selector?: (s: any) => unknown) =>
    typeof selector === "function" ? selector(mockChatState) : mockChatState
  ),
}));

vi.mock("@/stores/useChatModeStore", () => ({
  useChatModeStore: vi.fn((selector?: (s: any) => unknown) => {
    const state = {
      chatMode: "thinking",
      setChatMode: vi.fn(),
      temperature: 0.7,
      setTemperature: vi.fn(),
      retrievalMode: "auto",
      setRetrievalMode: vi.fn(),
      citationMode: "enabled",
      setCitationMode: vi.fn(),
    };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/stores/useLlmHealthStore", () => ({
  useLlmHealthStore: vi.fn((selector?: (s: any) => unknown) => {
    const state = { thinking: true, instant: true, refresh: vi.fn() };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/stores/useSettingsStore", () => ({
  useSettingsStore: vi.fn((selector?: (s: any) => unknown) => {
    const state = { formData: { default_chat_mode: "thinking" } };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: Object.assign(
    vi.fn((selector?: (s: any) => unknown) => {
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
  uploadDocument: vi.fn(),
  getDocumentStatus: vi.fn(),
}));

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({
    getRootProps: () => ({}),
    getInputProps: () => ({}),
    isDragActive: false,
    open: vi.fn(),
  }),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
  },
}));

describe("Composer toolbar keyboard reachability (L04 / AC8)", () => {
  const storage = new Map<string, string>();

  beforeEach(() => {
    vi.clearAllMocks();
    storage.clear();
    vi.mocked(localStorage.getItem).mockImplementation((key: string) => storage.get(key) ?? null);
    vi.mocked(localStorage.setItem).mockImplementation((key: string, value: string) => {
      storage.set(key, value);
    });
    vi.mocked(localStorage.removeItem).mockImplementation((key: string) => {
      storage.delete(key);
    });
    mockChatState.input = "";
    mockChatState.inputError = null;
    mockChatState.activeChatId = null;
  });

  it("slash and attach buttons are keyboard reachable", () => {
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    // tabIndex 0 keeps both toolbar controls in the natural tab order.
    expect(screen.getByRole("button", { name: "Open slash commands" }).tabIndex).toBe(0);
    expect(screen.getByRole("button", { name: "Attach file" }).tabIndex).toBe(0);
  });
});
