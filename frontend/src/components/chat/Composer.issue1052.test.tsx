/**
 * Issue #1052 regression tests — chat Composer extension allowlist (AC1/AC3).
 *
 * The Composer attach path (paste / drop / file picker funnels through
 * enqueueFiles) previously enqueued every file regardless of extension; only
 * the Documents dropzone consulted the server allowlist. Now enqueueFiles
 * reuses the shared serverAcceptsFilename predicate so a disallowed extension
 * is rejected client-side BEFORE enqueue, with the Documents dropzone's
 * message shape ("name (file type not allowed)"), and an unloaded allowlist
 * (settings === null) still defers to server validation.
 *
 * Harness follows Composer.large-input.test.tsx: the real Composer with
 * ambient stores mocked; the REAL upload store is stubbed so the enqueue
 * contract (addUploads/attachToChat calls) is observable without transfers.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act, createEvent } from "@testing-library/react";
import { Composer } from "./Composer";

const mockSettingsState = vi.hoisted(() => ({
  // null = settings not loaded. Flip to a SettingsResponse-shaped object per test.
  settings: null as null | { max_file_size_mb: number; allowed_extensions: string[] },
}));

const mockChatState = vi.hoisted(() => ({
  input: "",
  inputError: null as string | null,
  activeChatId: "42" as string | null,
  setInput: vi.fn((value: string) => {
    mockChatState.input = value;
  }),
  setInputError: vi.fn(),
}));

const mockVaultState = vi.hoisted(() => ({
  activeVaultId: 1 as number | null,
}));

vi.mock("@/stores/useChatStore", () => ({
  useChatStore: vi.fn((selector?: (s: any) => unknown) =>
    typeof selector === "function" ? selector(mockChatState) : mockChatState
  ),
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
  useSettingsStore: Object.assign(
    vi.fn((selector?: (s: any) => unknown) =>
      typeof selector === "function"
        ? selector({ formData: { default_chat_mode: "thinking" }, settings: mockSettingsState.settings })
        : { formData: { default_chat_mode: "thinking" }, settings: mockSettingsState.settings }
    ),
    { getState: () => ({ settings: mockSettingsState.settings }) }
  ),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: Object.assign(
    vi.fn((selector?: (s: any) => unknown) => {
      const state = {
        get activeVaultId() {
          return mockVaultState.activeVaultId;
        },
        vaults: [{ id: 1, name: "Test Vault", file_count: 1 }],
        getActiveVault: () => ({ id: 1, name: "Test Vault", file_count: 1 }),
      };
      return typeof selector === "function" ? selector(state) : state;
    }),
    { getState: () => ({ activeVaultId: mockVaultState.activeVaultId }) }
  ),
}));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/api",
  attachCsrfInterceptor: vi.fn(),
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

const toastError = vi.hoisted(() => vi.fn());
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: toastError,
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

/** Fire a paste carrying FILES on the composer textarea (the attach path). */
function pasteFiles(textarea: Element, files: File[]) {
  const clipboardData = {
    files,
    items: [],
    types: files.length > 0 ? ["Files"] : [],
    getData: () => "",
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

describe("Composer extension allowlist (issue #1052)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockChatState.input = "";
    mockChatState.inputError = null;
    mockChatState.activeChatId = "42";
    mockVaultState.activeVaultId = 1;
    mockSettingsState.settings = {
      max_file_size_mb: 100,
      allowed_extensions: [".pdf", ".txt", ".png"],
    };
  });

  afterEach(() => {
    vi.restoreAllMocks();
    mockSettingsState.settings = null;
  });

  it("rejects disallowed extension attachments client-side before enqueue with the dropzone message shape", async () => {
    const { addUploads, attachToChat } = await stubUploadStore();
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");

    const exe = new File(["payload"], "installer.exe", { type: "application/octet-stream" });
    await act(async () => {
      pasteFiles(textarea, [exe]);
    });

    // Rejected BEFORE the shared upload store — no enqueue, no attachment,
    // no wasted transfer that would end in a server 400.
    expect(addUploads).not.toHaveBeenCalled();
    expect(attachToChat).not.toHaveBeenCalled();
    // The exact per-file string the Documents dropzone surfaces
    // (UploadDropzone: `${file.name} (file type not allowed)`).
    expect(toastError).toHaveBeenCalledTimes(1);
    expect(toastError).toHaveBeenCalledWith("installer.exe (file type not allowed)");
  });

  it("enqueues only the allowed files of a mixed batch and reports each rejected file", async () => {
    const { addUploads, attachToChat } = await stubUploadStore();
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");

    const exe = new File(["x"], "bad.exe", { type: "application/octet-stream" });
    const pdf = new File(["doc"], "good.pdf", { type: "application/pdf" });
    await act(async () => {
      pasteFiles(textarea, [exe, pdf]);
    });

    expect(addUploads).toHaveBeenCalledTimes(1);
    const queued = addUploads.mock.calls[0][0] as File[];
    expect(queued.map((f) => f.name)).toEqual(["good.pdf"]);
    expect(attachToChat).toHaveBeenCalledTimes(1);
    expect(toastError).toHaveBeenCalledTimes(1);
    expect(toastError).toHaveBeenCalledWith("bad.exe (file type not allowed)");
  });

  it("defers to server validation while settings have not loaded (no client-side rejection)", async () => {
    const { addUploads } = await stubUploadStore();
    mockSettingsState.settings = null;
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);
    const textarea = screen.getByLabelText("Message input");

    const exe = new File(["x"], "early.exe", { type: "application/octet-stream" });
    await act(async () => {
      pasteFiles(textarea, [exe]);
    });

    // Settings not loaded → allowlist unknown → today's behavior: the server
    // validates (same gate as UploadDropzone's allowedExtensionsLoaded).
    expect(addUploads).toHaveBeenCalledTimes(1);
    expect((addUploads.mock.calls[0][0] as File[]).map((f) => f.name)).toEqual(["early.exe"]);
    expect(toastError).not.toHaveBeenCalled();
  });
});
