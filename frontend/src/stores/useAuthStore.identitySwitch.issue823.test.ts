// Issue #823 identity-boundary hardening (unfrozen, plan-critic rounds 2-6).
//
// Beyond the frozen C4 check (logout clears rows/activeChatId/activeSessionId),
// this suite pins the full principal-transition contract of the approved plan:
//   - logout transport timeout still clears chat state (resets precede the
//     timeout re-throw);
//   - different-principal login (with user in the response) clears every
//     content-bearing surface: transcript rows, unsent composer input,
//     persisted localStorage drafts, shell session id/title/evidence, chat
//     attachments, upload rows and the vault pointer;
//   - a user-less login response (principal unknown until fetchMe) is
//     treated as a boundary at the publication instant;
//   - FAILED login never crosses the boundary (chat state intact);
//   - SAME-principal re-login (with user in the response) does not clear;
//   - asynchronous upload writers die at the boundary: an in-flight A-owned
//     transfer completing after the switch produces no toast, no state
//     write, and no queue continuation; a queued A file never dispatches.
//
// Transport mocking mirrors useAuthStore.logoutChatState.issue823.test.ts
// (frozen C4); the chat/shell/upload stores under assertion are REAL.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const { mockPostFn, mockGetFn, mockPatchFn, mockResetCsrfToken, mockEnsureCsrfToken, mockResetSubpathRefreshDiagnostic, mockResetCitationReport, mockUploadDocument } = vi.hoisted(() => ({
  mockPostFn: vi.fn(),
  mockGetFn: vi.fn(),
  mockPatchFn: vi.fn(),
  mockResetCsrfToken: vi.fn(),
  mockEnsureCsrfToken: vi.fn().mockResolvedValue("mock-csrf-token"),
  mockResetSubpathRefreshDiagnostic: vi.fn(),
  mockResetCitationReport: vi.fn(),
  mockUploadDocument: vi.fn(),
}));

const { mockJwtTokenHolder } = vi.hoisted(() => ({
  mockJwtTokenHolder: { value: null as string | null },
}));

vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => ({
      get: mockGetFn,
      post: mockPostFn,
      patch: mockPatchFn,
      put: vi.fn(),
      delete: vi.fn(),
      interceptors: {
        request: { use: vi.fn((cb) => cb) },
        response: { use: vi.fn((cb) => cb) },
      },
    })),
  },
}));

vi.mock("@/lib/api/core", () => ({
  getJwtAccessToken: vi.fn(() => mockJwtTokenHolder.value),
  onJwtAccessTokenPublished: vi.fn(() => () => {}),
}));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/api",
  setJwtAccessToken: vi.fn((token: string | null) => {
    mockJwtTokenHolder.value = token;
  }),
  getJwtAccessToken: vi.fn(() => mockJwtTokenHolder.value),
  refreshAccessToken: vi.fn(),
  resetCsrfToken: mockResetCsrfToken,
  ensureCsrfToken: mockEnsureCsrfToken,
  ensureCsrfTokenPhysical: mockEnsureCsrfToken,
  resetSubpathRefreshDiagnostic: mockResetSubpathRefreshDiagnostic,
  attachCsrfInterceptor: vi.fn(),
  uploadDocument: mockUploadDocument,
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  },
}));

vi.mock("@/lib/api/onboarding", () => ({
  resetCitationReport: mockResetCitationReport,
  getOnboardingMilestones: vi.fn(),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: {
    getState: vi.fn(() => ({ fetchVaults: vi.fn() })),
  },
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}));

import { useAuthStore, resetInitState } from "./useAuthStore";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useUploadStore } from "@/stores/useUploadStore";
import { DRAFT_PREFIX } from "@/lib/chatDrafts";
import { AuthTransportTimeoutError } from "@/lib/api/auth-lifecycle";
import { toast } from "sonner";

const USER_A = { id: 11, username: "usera", full_name: "User A", role: "admin" as const, is_active: true };
const USER_B = { id: 22, username: "userb", full_name: "User B", role: "admin" as const, is_active: true };

/** Functional in-memory localStorage (vi.fn mock lacks key/length iteration). */
function installLocalStorage() {
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

function seedUserAChatState(store: Map<string, string>) {
  useAuthStore.setState({
    user: USER_A,
    accessToken: "jwt-a",
    isAuthenticated: true,
    authMode: "jwt",
  });
  useChatStore.getState().loadChat("7", [
    { id: "row-user", role: "user", content: "user A question" },
    { id: "row-assistant", role: "assistant", content: "user A answer" },
  ]);
  useChatStore.getState().setInput("user A's unsent draft");
  useChatShellStore.getState().setActiveSessionId("7");
  useChatShellStore.getState().setActiveSessionTitle("A's private session");
  useChatShellStore.getState().setSessionSearchQuery("A's private search");
  useChatShellStore.setState({ pinnedSessionIds: [7] });
  store.set(`${DRAFT_PREFIX}7`, "user A's persisted draft");
  store.set(`${DRAFT_PREFIX}new`, "user A's new-chat draft");
  useUploadStore.setState({
    uploads: [
      {
        id: "upl-1",
        file: new File(["a-bytes"], "a-secret.txt", { type: "text/plain" }),
        uploadProgress: 50,
        progress: 50,
        processingProgress: null,
        wikiProgress: null,
        status: "uploading",
        statusSeen: false,
        startedAt: Date.now(),
      },
    ],
    chatAttachmentIds: ["upl-1"],
    activeVaultId: 1,
    isProcessing: true,
  });
}

describe("useAuthStore identity-boundary reset (issue #823 hardening)", () => {
  let storage: Map<string, string>;

  beforeEach(() => {
    vi.clearAllMocks();
    mockEnsureCsrfToken.mockResolvedValue("mock-csrf-token");
    mockJwtTokenHolder.value = null;
    resetInitState();
    useAuthStore.setState({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isInitialized: false,
      initializationFailed: false,
      isLoading: false,
      needsSetup: false,
      authMode: "unknown",
    });
    useChatStore.getState().newChat();
    useChatShellStore.getState().setActiveSessionId(null);
    useChatShellStore.getState().setActiveSessionTitle(null);
    useUploadStore.getState().resetForIdentitySwitch();
    storage = installLocalStorage();
  });

  afterEach(() => {
    resetInitState();
  });

  it("logout transport timeout still clears the chat surface before re-throwing", async () => {
    storage = installLocalStorage();
    seedUserAChatState(storage);
    mockPostFn.mockRejectedValueOnce(new AuthTransportTimeoutError("deadline exceeded"));

    const { logout } = useAuthStore.getState();
    await expect(logout()).rejects.toBeInstanceOf(AuthTransportTimeoutError);

    expect(useChatStore.getState().activeChatId).toBeNull();
    expect(useChatStore.getState().messageIds).toEqual([]);
    expect(useChatShellStore.getState().activeSessionId).toBeNull();
  });

  it("different-principal login clears every content-bearing chat surface at publication", async () => {
    seedUserAChatState(storage);

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-b", user: USER_B },
    });
    mockGetFn.mockResolvedValueOnce({ data: USER_B }); // fetchMe not needed (user present)

    const { login } = useAuthStore.getState();
    await login("userb", "pw");

    expect(useAuthStore.getState().user?.id).toBe(22);
    // Transcript + composer input + edit surface
    expect(useChatStore.getState().activeChatId).toBeNull();
    expect(useChatStore.getState().messageIds).toEqual([]);
    expect(useChatStore.getState().input).toBe("");
    // Persisted drafts: every prefixed key gone (session AND "new")
    expect(Array.from(storage.keys()).every((k) => !k.startsWith(DRAFT_PREFIX))).toBe(true);
    // Shell surface (incl. review-round-1 additions: search query + pins)
    expect(useChatShellStore.getState().activeSessionId).toBeNull();
    expect(useChatShellStore.getState().activeSessionTitle).toBeNull();
    expect(useChatShellStore.getState().sessionSearchQuery).toBe("");
    expect(useChatShellStore.getState().pinnedSessionIds).toEqual([]);
    // Upload surface: rows, attachment registrations, vault pointer
    expect(useUploadStore.getState().uploads).toEqual([]);
    expect(useUploadStore.getState().chatAttachmentIds).toEqual([]);
    expect(useUploadStore.getState().activeVaultId).toBeNull();
  });

  it("user-less login response is treated as a boundary at publication", async () => {
    seedUserAChatState(storage);

    mockPostFn.mockResolvedValueOnce({ data: { access_token: "jwt-b" } }); // no user
    let resolveFetchMe: (v: { data: typeof USER_B }) => void = () => {};
    mockGetFn.mockImplementationOnce(
      () => new Promise((res) => { resolveFetchMe = res; })
    );

    const loginPromise = useAuthStore.getState().login("userb", "pw");
    // The fetchMe await is still pending, but the boundary already fired at
    // credential publication: A's surfaces are gone BEFORE fetchMe resolves.
    await vi.waitFor(() => {
      expect(useChatStore.getState().activeChatId).toBeNull();
      expect(useUploadStore.getState().uploads).toEqual([]);
    });
    resolveFetchMe({ data: USER_B });
    await loginPromise;
    expect(useChatStore.getState().input).toBe("");
  });

  it("failed login never crosses the boundary (chat surface intact)", async () => {
    seedUserAChatState(storage);
    mockPostFn.mockRejectedValueOnce(new Error("bad credentials"));

    await expect(useAuthStore.getState().login("userb", "wrong")).rejects.toBeInstanceOf(Error);

    expect(useChatStore.getState().activeChatId).toBe("7");
    expect(useChatStore.getState().messageIds).toHaveLength(2);
    expect(useChatStore.getState().input).toBe("user A's unsent draft");
    expect(useUploadStore.getState().uploads).toHaveLength(1);
    expect(useUploadStore.getState().chatAttachmentIds).toEqual(["upl-1"]);
  });

  it("same-principal re-login (user in response) does not clear the chat surface", async () => {
    seedUserAChatState(storage);
    mockPostFn.mockResolvedValueOnce({ data: { access_token: "jwt-a2", user: USER_A } });

    await useAuthStore.getState().login("usera", "pw");

    expect(useChatStore.getState().activeChatId).toBe("7");
    expect(useChatStore.getState().messageIds).toHaveLength(2);
    expect(useUploadStore.getState().uploads).toHaveLength(1);
  });

  it("in-flight A-owned uploads completing after the switch are fully suppressed", async () => {
    seedUserAChatState(storage);
    const uploadResolvers: Array<(v: { id: number }) => void> = [];
    mockUploadDocument.mockImplementation(
      () => new Promise((res) => { uploadResolvers.push(res as (v: { id: number }) => void); })
    );
    // Re-arm two pending transfers through the real queue path.
    useUploadStore.setState({
      uploads: [
        {
          id: "upl-1",
          file: new File(["a-bytes"], "a-secret.txt", { type: "text/plain" }),
          uploadProgress: 0,
          progress: 0,
          processingProgress: null,
          wikiProgress: null,
          status: "pending",
          statusSeen: false,
          startedAt: Date.now(),
        },
        {
          id: "upl-2",
          file: new File(["b-bytes"], "a-queued.txt", { type: "text/plain" }),
          uploadProgress: 0,
          progress: 0,
          processingProgress: null,
          wikiProgress: null,
          status: "pending",
          statusSeen: false,
          startedAt: Date.now(),
        },
      ],
      chatAttachmentIds: ["upl-1", "upl-2"],
      activeVaultId: 1,
    });
    const dispatched = useUploadStore.getState().processQueue();
    void dispatched;
    await vi.waitFor(() => {
      // The bounded pool admits both A-owned files (concurrency >= 2).
      expect(mockUploadDocument.mock.calls.length).toBeGreaterThanOrEqual(1);
    });
    const dispatchedCount = mockUploadDocument.mock.calls.length;

    // Identity switch mid-flight (different-principal login).
    mockPostFn.mockResolvedValueOnce({ data: { access_token: "jwt-b", user: USER_B } });
    await useAuthStore.getState().login("userb", "pw");

    // Stale completions: every side effect must be suppressed.
    for (const resolve of uploadResolvers) resolve({ id: 999 });
    await vi.waitFor(() => {
      expect(useUploadStore.getState().uploads).toEqual([]); // rows stay cleared
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
    // No queued A item ever dispatched past the boundary.
    expect(mockUploadDocument.mock.calls.length).toBe(dispatchedCount);
  });
});
