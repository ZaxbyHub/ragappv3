// Regression (issue #507 / composer slash menu): the toolbar's slash button
// must open the command menu directly — no typing required. Copy of the
// minimal mock harness from Composer.draft.test.tsx.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
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
  useChatStore: vi.fn(() => mockChatState),
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

describe("Composer slash commands button", () => {
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

  it("Open slash commands button opens the menu without typing", () => {
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    // Menu is closed initially.
    expect(screen.queryByRole("listbox", { name: "Slash commands" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Open slash commands"));

    // The slash menu appears with the full command list (no filter query).
    expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
    expect(screen.getByText("/summarize")).toBeInTheDocument();
    expect(screen.getByText("/compare")).toBeInTheDocument();
    expect(screen.getByText("/timeline")).toBeInTheDocument();
    expect(screen.getByText("/actions")).toBeInTheDocument();

    // The textarea reflects the inserted "/" and exposes the expanded state.
    expect(mockChatState.setInput).toHaveBeenCalledWith("/");
    expect(screen.getByLabelText("Message input")).toHaveAttribute("aria-expanded", "true");
  });
});
