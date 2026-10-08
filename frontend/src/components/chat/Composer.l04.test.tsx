// Issue #775 AC8: the two toolbar controls must be keyboard reachable and
// remain wired to their real Composer behaviors. This keeps the existing
// lightweight store harness from Composer.slash-button.test.tsx.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const mockChatState = vi.hoisted(() => ({
  input: "",
  inputError: null as string | null,
  activeChatId: null as string | null,
  setInput: vi.fn((value: string) => {
    mockChatState.input = value;
  }),
}));

const mockDropzone = vi.hoisted(() => ({ open: vi.fn() }));

vi.mock("@/stores/useChatStore", () => ({
  useChatStore: vi.fn((selector?: (state: typeof mockChatState) => unknown) =>
    typeof selector === "function" ? selector(mockChatState) : mockChatState
  ),
}));

vi.mock("@/stores/useChatModeStore", () => ({
  useChatModeStore: vi.fn((selector?: (state: Record<string, unknown>) => unknown) => {
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
  useLlmHealthStore: vi.fn((selector?: (state: Record<string, unknown>) => unknown) => {
    const state = { thinking: true, instant: true, refresh: vi.fn() };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/stores/useSettingsStore", () => ({
  useSettingsStore: vi.fn((selector?: (state: Record<string, unknown>) => unknown) => {
    const state = { formData: { default_chat_mode: "thinking" } };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: Object.assign(
    vi.fn((selector?: (state: Record<string, unknown>) => unknown) => {
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

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({
    getRootProps: () => ({}),
    getInputProps: () => ({}),
    isDragActive: false,
    open: mockDropzone.open,
  }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

import { Composer } from "./Composer";

describe("#775 Composer keyboard reachability", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockChatState.input = "";
    mockChatState.inputError = null;
  });

  it("slash and attach buttons are keyboard reachable", () => {
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    expect(screen.getByRole("button", { name: "Open slash commands" })).toHaveProperty("tabIndex", 0);
    expect(screen.getByRole("button", { name: "Attach file" })).toHaveProperty("tabIndex", 0);
  });

  it("slash and attach buttons execute their real Composer actions", () => {
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Open slash commands" }));
    expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
    expect(mockChatState.setInput).toHaveBeenCalledWith("/");

    fireEvent.click(screen.getByRole("button", { name: "Attach file" }));
    expect(mockDropzone.open).toHaveBeenCalledTimes(1);
  });
});
