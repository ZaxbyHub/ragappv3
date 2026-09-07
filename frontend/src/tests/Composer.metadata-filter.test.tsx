/**
 * Issue #510 AC-16: metadata-filter controls in the composer toolbar and
 * their serialization into the chat stream request body.
 *
 * 1. The four filter inputs render with their aria-labels.
 * 2. Editing an input updates useChatModeStore (the REAL store is used, so
 *    this asserts the Composer→store wiring, not a mock).
 * 3. Inputs are disabled while isStreaming, like the adjacent selectors.
 * 4. Send path: store values serialize as metadata_filter on the POST to
 *    /chat/stream — only non-empty fields (tags split/trimmed, empty entries
 *    dropped); an author-only filter sends {author} and nothing else; an
 *    empty filter omits the key entirely. Asserted against the actual fetch
 *    request body (real chatStream; fetch stubbed, stream never resolves).
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act, renderHook } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import { Composer } from "@/components/chat/Composer";
import { useSendMessage } from "@/hooks/useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";

// Stub fetch for the send-path tests. The chat/stream response never
// resolves; each test asserts the recorded request, then stops the send so
// in-flight guards reset and nothing is persisted.
const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

// Deterministic cookie jar — ensureCsrfToken must resolve from the cookie
// without a network round-trip (mirrors lib/api.csrf.test.ts).
let mockCookies = "";
Object.defineProperty(document, "cookie", {
  get: () => mockCookies,
  set: (val: string) => {
    mockCookies = val;
  },
  configurable: true,
});

// Mocked stores must stay callable AND expose getState(): useSendMessage
// reads them via getState() at send time while the Composer subscribes.
vi.mock("@/stores/useLlmHealthStore", () => {
  const state = {
    thinking: true,
    instant: true,
    lastCheckedAt: null as number | null,
    refreshing: false,
    refresh: vi.fn(),
  };
  const useLlmHealthStore = Object.assign(
    vi.fn((selector?: (s: typeof state) => unknown) =>
      typeof selector === "function" ? selector(state) : state
    ),
    { getState: () => state }
  );
  return { useLlmHealthStore };
});

vi.mock("@/stores/useSettingsStore", () => {
  const state = { formData: { default_chat_mode: "thinking" as const } };
  const useSettingsStore = Object.assign(
    vi.fn((selector?: (s: typeof state) => unknown) =>
      typeof selector === "function" ? selector(state) : state
    ),
    { getState: () => state }
  );
  return { useSettingsStore };
});

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

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({
    getRootProps: () => ({}),
    getInputProps: () => ({}),
    isDragActive: false,
    open: vi.fn(),
  }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

// NOTE: useChatStore, useChatModeStore, and @/lib/api are deliberately NOT
// mocked — the send-path tests exercise the real store → useSendMessage →
// chatStream → fetch serialization chain end to end.

beforeEach(() => {
  vi.clearAllMocks();
  // The chat stream never resolves; only the recorded request is inspected.
  fetchMock.mockImplementation(() => new Promise<Response>(() => {}));
  mockCookies = "X-CSRF-Token=test-csrf-token";
  useChatStore.setState({
    messageIds: [],
    messagesById: {},
    streamingMessageId: null,
    input: "",
    isStreaming: false,
    abortFn: null,
    inputError: null,
    activeChatId: null,
    pendingTurnPersist: null,
  });
  useChatModeStore.setState({
    chatMode: null,
    metadataFilterDateFrom: "",
    metadataFilterDateTo: "",
    metadataFilterTags: "",
    metadataFilterAuthor: "",
  });
});

describe("Composer metadata-filter controls (issue #510 AC-16)", () => {
  it("renders the four filter inputs with aria-labels", () => {
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    expect(screen.getByLabelText("Filter from date")).toBeInTheDocument();
    expect(screen.getByLabelText("Filter to date")).toBeInTheDocument();
    expect(screen.getByLabelText("Filter tags")).toBeInTheDocument();
    expect(screen.getByLabelText("Filter author")).toBeInTheDocument();
  });

  it("binds the inputs to useChatModeStore — editing updates store state", async () => {
    const user = userEvent.setup();
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    await user.type(screen.getByLabelText("Filter author"), "davis");
    expect(useChatModeStore.getState().metadataFilterAuthor).toBe("davis");

    fireEvent.change(screen.getByLabelText("Filter tags"), {
      target: { value: "alpha, beta" },
    });
    expect(useChatModeStore.getState().metadataFilterTags).toBe("alpha, beta");

    fireEvent.change(screen.getByLabelText("Filter from date"), {
      target: { value: "2026-01-01" },
    });
    expect(useChatModeStore.getState().metadataFilterDateFrom).toBe("2026-01-01");

    fireEvent.change(screen.getByLabelText("Filter to date"), {
      target: { value: "2026-06-30" },
    });
    expect(useChatModeStore.getState().metadataFilterDateTo).toBe("2026-06-30");
  });

  it("resetMetadataFilter clears every filter field", () => {
    useChatModeStore.setState({
      metadataFilterDateFrom: "2026-01-01",
      metadataFilterDateTo: "2026-06-30",
      metadataFilterTags: "alpha",
      metadataFilterAuthor: "davis",
    });

    useChatModeStore.getState().resetMetadataFilter();

    const state = useChatModeStore.getState();
    expect(state.metadataFilterDateFrom).toBe("");
    expect(state.metadataFilterDateTo).toBe("");
    expect(state.metadataFilterTags).toBe("");
    expect(state.metadataFilterAuthor).toBe("");
  });

  it("disables the filter inputs while streaming, like the adjacent selectors", () => {
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={true} />);

    expect(screen.getByLabelText("Filter from date")).toBeDisabled();
    expect(screen.getByLabelText("Filter to date")).toBeDisabled();
    expect(screen.getByLabelText("Filter tags")).toBeDisabled();
    expect(screen.getByLabelText("Filter author")).toBeDisabled();
  });
});

describe("metadata_filter serialization into the chat stream request (issue #510 AC-16)", () => {
  /** Send through the REAL useSendMessage path and capture the POST body. */
  const sendAndCaptureBody = async (): Promise<Record<string, unknown>> => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    // Existing session id → no createChatSession round-trip; only the
    // stubbed fetch (chat/stream) is exercised.
    useChatStore.setState({ activeChatId: "42", input: "filter me" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));
    await act(async () => {
      await result.current.handleSend();
    });

    let body: Record<string, unknown> | undefined;
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).includes("/chat/stream")
      );
      expect(call).toBeDefined();
      body = JSON.parse(String(call[1].body)) as Record<string, unknown>;
    });

    // Stop the never-resolving stream so guards reset and nothing persists.
    act(() => {
      result.current.handleStop();
    });
    return body as Record<string, unknown>;
  };

  it("sends only non-empty fields: dates as-is, tags split/trimmed with empties dropped", async () => {
    useChatModeStore.setState({
      metadataFilterDateFrom: "2026-01-01",
      metadataFilterTags: " alpha , beta ,, ",
      metadataFilterAuthor: "davis",
    });

    const body = await sendAndCaptureBody();

    expect(body.metadata_filter).toEqual({
      date_from: "2026-01-01",
      tags: ["alpha", "beta"],
      author: "davis",
    });
    expect(body.metadata_filter).not.toHaveProperty("date_to");
  });

  it("author-only filter serializes as { author } with no other keys", async () => {
    useChatModeStore.setState({ metadataFilterAuthor: "davis" });

    const body = await sendAndCaptureBody();

    expect(body.metadata_filter).toEqual({ author: "davis" });
    expect(body.metadata_filter).not.toHaveProperty("date_from");
    expect(body.metadata_filter).not.toHaveProperty("date_to");
    expect(body.metadata_filter).not.toHaveProperty("tags");
  });

  it("omits metadata_filter entirely when every field is empty", async () => {
    const body = await sendAndCaptureBody();

    expect(body).not.toHaveProperty("metadata_filter");
  });
});
