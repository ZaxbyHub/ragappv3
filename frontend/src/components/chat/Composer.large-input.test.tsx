/**
 * Issue #616 regression tests — large chat input.
 *
 * Covers the shipped behaviors fixed for the reported 20k-character
 * composer failure:
 *  - large plain-text pastes (> LARGE_PASTE_THRESHOLD) become text/plain
 *    File attachments instead of flooding the textarea;
 *  - small pastes stay inline;
 *  - draft persistence is debounced (leading+trailing) — a burst of input
 *    changes never rewrites the full draft string per change;
 *  - drafts beyond MAX_DRAFT_CHARS (= MAX_INPUT_LENGTH) are never
 *    persisted (boundary pinned at 99,999 / 100,001);
 *  - the inline send gate accepts 20,000-char input and visibly rejects
 *    100,001-char input;
 *  - sending clears the persisted draft synchronously.
 *
 * The component under test is the real Composer; ambient stores and the
 * HTTP layer are mocked per Composer.draft.test.tsx's pattern. The mocked
 * useChatStore does not subscribe, so render-output assertions pre-set
 * mockChatState.input before render (the same pattern the TranscriptPane
 * counter tests use) instead of relying on change-driven re-renders.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act, createEvent } from "@testing-library/react";
import { Composer, LARGE_PASTE_THRESHOLD } from "./Composer";
import { MAX_INPUT_LENGTH } from "@/hooks/useSendMessage";

const mockChatState = vi.hoisted(() => ({
  input: "",
  inputError: null as string | null,
  activeChatId: null as string | null,
  setInput: vi.fn((value: string) => {
    mockChatState.input = value;
  }),
  setInputError: vi.fn(),
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
    const state = { formData: { default_chat_mode: "thinking" }, settings: null };
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
  getDocumentStatuses: vi.fn().mockResolvedValue({ results: [] }),
  chatStream: vi.fn(),
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  addChatMessagesBatchKeepalive: vi.fn(),
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
    info: vi.fn(),
  },
}));

/** Fire a plain-text paste (no files) and return the event for inspection. */
function pastePlainText(textarea: Element, text: string) {
  const clipboardData = {
    files: [] as File[],
    items: [],
    types: ["text/plain"],
    getData: (type: string) => (type === "text/plain" ? text : ""),
  };
  const event = createEvent.paste(textarea, { clipboardData });
  fireEvent(textarea, event);
  return event;
}

/**
 * Spy on the REAL upload store's live state (module not mocked) so the
 * Composer's enqueue contract is observable without running transfers.
 */
async function stubUploadStore() {
  const mod = await import("@/stores/useUploadStore");
  const store = (
    mod as { useUploadStore: { getState: () => any; setState: (s: any) => void } }
  ).useUploadStore;
  store.setState({
    uploads: [],
    isProcessing: false,
    activeVaultId: null,
    chatAttachmentIds: [],
  });
  const state = store.getState();
  const addUploads = vi
    .spyOn(state, "addUploads")
    .mockImplementation(((files: File[]) => files.map((_, i) => `up-1-${i}`)) as never);
  const attachToChat = vi
    .spyOn(state, "attachToChat")
    .mockImplementation((() => {}) as never);
  return { addUploads, attachToChat };
}

describe("Composer large-input handling (issue #616)", () => {
  const storage = new Map<string, string>();
  let draftWrites: number;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    storage.clear();
    draftWrites = 0;
    vi.mocked(localStorage.getItem).mockImplementation((key: string) => storage.get(key) ?? null);
    vi.mocked(localStorage.setItem).mockImplementation((key: string, value: string) => {
      storage.set(key, value);
      if (key.startsWith("ragapp_chat_draft_")) draftWrites += 1;
    });
    vi.mocked(localStorage.removeItem).mockImplementation((key: string) => {
      storage.delete(key);
    });
    vi.mocked(localStorage.clear).mockImplementation(() => {
      storage.clear();
    });
    mockChatState.input = "";
    mockChatState.inputError = null;
    mockChatState.activeChatId = "42";
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("a >threshold plain-text paste becomes ONE text/plain File upload, keeping the textarea empty", async () => {
    const { addUploads, attachToChat } = await stubUploadStore();
    const big = "p".repeat(20_000);
    expect(big.length).toBeGreaterThan(LARGE_PASTE_THRESHOLD);

    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input") as HTMLTextAreaElement;

    let event: ReturnType<typeof createEvent.paste>;
    await act(async () => {
      event = pastePlainText(textarea, big);
    });

    expect(event!.defaultPrevented).toBe(true);
    expect(addUploads).toHaveBeenCalledTimes(1);
    const queued = addUploads.mock.calls[0][0] as File[];
    expect(queued).toHaveLength(1);
    expect(queued[0].name).toMatch(/^pasted-text-\d+\.txt$/);
    expect(queued[0].type).toBe("text/plain");
    const text = await act(() => queued[0].text());
    expect(text).toBe(big);
    expect(attachToChat).toHaveBeenCalledTimes(1);
    expect(mockChatState.input).not.toContain(big.slice(0, 100));
    expect(textarea.value).toBe("");
  });

  it("a small plain-text paste stays inline (no interception, no upload)", async () => {
    const { addUploads } = await stubUploadStore();
    const small = "s".repeat(100);
    expect(small.length).toBeLessThanOrEqual(LARGE_PASTE_THRESHOLD);

    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input") as HTMLTextAreaElement;

    let event: ReturnType<typeof createEvent.paste>;
    await act(async () => {
      event = pastePlainText(textarea, small);
    });

    expect(event!.defaultPrevented).toBe(false);
    expect(addUploads).not.toHaveBeenCalled();
  });

  it("debounces draft persistence: a burst of changes writes at most a couple of times, then persists the final value", () => {
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");

    // The first change after idle writes synchronously (leading edge).
    fireEvent.change(textarea, { target: { value: "first" } });
    expect(storage.get("ragapp_chat_draft_42")).toBe("first");

    let value = "first";
    for (let i = 1; i <= 30; i += 1) {
      value = `${value}word${i} `;
      fireEvent.change(textarea, { target: { value } });
    }
    expect(draftWrites).toBeLessThanOrEqual(3);

    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(storage.get("ragapp_chat_draft_42")).toBe(value);
    expect(draftWrites).toBeLessThanOrEqual(4);
  });

  it("pins the persisted-draft boundary: 99,999 persists, 100,001 does not", () => {
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");

    fireEvent.change(textarea, { target: { value: "u".repeat(99_999) } });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(storage.get("ragapp_chat_draft_42")).toHaveLength(99_999);

    fireEvent.change(textarea, { target: { value: "v".repeat(100_001) } });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(storage.has("ragapp_chat_draft_42")).toBe(false);
  });

  it("accepts 20,000-char input for send and visibly rejects 100,001", () => {
    const onSend = vi.fn();
    // Render-output assertions pre-set input (the mocked store does not
    // subscribe, so change events alone do not re-render).
    mockChatState.input = "w".repeat(20_000);
    const { rerender } = render(
      <Composer onSend={onSend} onStop={vi.fn()} isStreaming={false} />
    );
    expect(MAX_INPUT_LENGTH).toBe(100_000);
    expect(screen.getByLabelText("Send message")).toBeEnabled();

    mockChatState.input = "x".repeat(100_001);
    rerender(<Composer onSend={onSend} onStop={vi.fn()} isStreaming={false} />);
    const sendButton = screen.getByLabelText("Send message");
    expect(sendButton).toBeDisabled();
    // The disabled button swallows clicks; Enter is the real user path.
    fireEvent.keyDown(screen.getByLabelText("Message input"), {
      key: "Enter",
      shiftKey: false,
    });
    expect(onSend).not.toHaveBeenCalled();
    // The block is not silent: the store records the over-cap error.
    expect(mockChatState.setInputError).toHaveBeenCalledWith(
      expect.stringMatching(/exceeds maximum length of 100,000/i)
    );
  });

  it("sending clears the persisted draft synchronously", () => {
    const onSend = vi.fn(() => {
      mockChatState.input = "";
    });
    const { rerender } = render(
      <Composer onSend={onSend} onStop={vi.fn()} isStreaming={false} />
    );
    const textarea = screen.getByLabelText("Message input");

    fireEvent.change(textarea, { target: { value: "send me" } });
    expect(storage.get("ragapp_chat_draft_42")).toBe("send me");

    // The mocked store does not subscribe: re-render so the submit
    // handler's closure sees the changed input.
    rerender(<Composer onSend={onSend} onStop={vi.fn()} isStreaming={false} />);
    fireEvent.click(screen.getByLabelText("Send message"));
    expect(storage.has("ragapp_chat_draft_42")).toBe(false);
    expect(onSend).toHaveBeenCalledTimes(1);
  });
});
