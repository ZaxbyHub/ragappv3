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
    const state = { chatMode: "thinking", setChatMode: vi.fn() };
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

describe("Composer draft persistence", () => {
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
    vi.mocked(localStorage.clear).mockImplementation(() => {
      storage.clear();
    });
    mockChatState.input = "";
    mockChatState.inputError = null;
    mockChatState.activeChatId = null;
  });

  it("loads the new-chat draft on mount", () => {
    storage.set("ragapp_chat_draft_new", "unsent new-chat draft");

    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    expect(mockChatState.setInput).toHaveBeenCalledWith("unsent new-chat draft");
  });

  it("saves and clears the active session draft", () => {
    mockChatState.activeChatId = "42";
    const onSend = vi.fn(() => {
      mockChatState.input = "";
    });
    const { rerender } = render(<Composer onSend={onSend} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");

    fireEvent.change(textarea, { target: { value: "persist me" } });
    expect(storage.get("ragapp_chat_draft_42")).toBe("persist me");

    rerender(<Composer onSend={onSend} onStop={vi.fn()} isStreaming={false} />);
    fireEvent.click(screen.getByLabelText("Send message"));
    expect(storage.has("ragapp_chat_draft_42")).toBe(false);
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("does not reload the same session draft on rerender after the user edits", () => {
    mockChatState.activeChatId = "7";
    storage.set("ragapp_chat_draft_7", "stored draft");
    const { rerender } = render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    expect(mockChatState.setInput).toHaveBeenCalledWith("stored draft");
    mockChatState.setInput.mockClear();

    fireEvent.change(screen.getByLabelText("Message input"), { target: { value: "typed after load" } });
    rerender(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    expect(mockChatState.setInput).toHaveBeenCalledTimes(1);
    expect(mockChatState.setInput).toHaveBeenCalledWith("typed after load");
  });

  it("does not reload when the active chat id changes but still resolves to the new-chat draft key", () => {
    mockChatState.activeChatId = null;
    storage.set("ragapp_chat_draft_new", "new-chat draft");
    const { rerender } = render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    expect(mockChatState.setInput).toHaveBeenCalledWith("new-chat draft");
    mockChatState.setInput.mockClear();
    vi.mocked(localStorage.getItem).mockClear();

    mockChatState.activeChatId = undefined as unknown as null;
    rerender(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    expect(localStorage.getItem).not.toHaveBeenCalled();
    expect(mockChatState.setInput).not.toHaveBeenCalled();
  });

  it("loads the next session draft when activeChatId changes", () => {
    mockChatState.activeChatId = "1";
    storage.set("ragapp_chat_draft_1", "first session");
    storage.set("ragapp_chat_draft_2", "second session");
    const { rerender } = render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    mockChatState.activeChatId = "2";
    rerender(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    expect(mockChatState.setInput).toHaveBeenCalledWith("first session");
    expect(mockChatState.setInput).toHaveBeenCalledWith("second session");
  });
});
