// Issue #1032 C4: an identity switch must clear the persisted localStorage
// canvas drafts. CanvasPage.tsx persists unsaved canvas editor text under
// `canvas-draft:<artifactUid>` (canvasDraftKey, CanvasPage.tsx:63-89) — a key
// namespace with NO principal segment. resetChatStateForIdentitySwitch only
// wipes the chat draft namespace (clearAllChatDrafts), so user A's canvas
// drafts survive a different-principal login in the same tab and user B's
// canvas editor rehydrates them (CanvasPage reads the key on mount).
//
// The clear must also stay SCOPED: an unrelated key the app owns for
// non-per-principal purposes survives, proving the fix enumerates
// canvas-draft keys rather than calling storage.clear().
//
// Transport mocking mirrors useAuthStore.identitySwitch.issue823.test.ts;
// the auth store under test is REAL and the login below is a REAL
// different-principal identity switch.

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
import { getDraftKey } from "@/lib/chatDrafts";

const USER_A = { id: 11, username: "usera", full_name: "User A", role: "admin" as const, is_active: true };
const USER_B = { id: 22, username: "userb", full_name: "User B", role: "admin" as const, is_active: true };

// Mirrors canvasDraftKey() in CanvasPage.tsx (not exported, so the literal
// prefix is the contract): `canvas-draft:<artifactUid>`.
const CANVAS_DRAFT_PREFIX = "canvas-draft:";

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

describe("useAuthStore identity scope — persisted canvas drafts (issue #1032)", () => {
  let storage: Map<string, string>;

  beforeEach(() => {
    vi.clearAllMocks();
    mockGetFn.mockReset();
    mockPostFn.mockReset();
    mockUploadDocument.mockReset();
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
    storage = installLocalStorage();
  });

  afterEach(() => {
    resetInitState();
  });

  it("different-principal login removes every canvas draft key but no unrelated key", async () => {
    useAuthStore.setState({
      user: USER_A,
      accessToken: "jwt-a",
      isAuthenticated: true,
      authMode: "jwt",
    });
    storage.set(`${CANVAS_DRAFT_PREFIX}art-1`, "user A's unsaved canvas text 1");
    storage.set(`${CANVAS_DRAFT_PREFIX}art-2`, "user A's unsaved canvas text 2");
    // Chat draft key built through the lib (ragapp_chat_draft_11:7) — a
    // per-principal sibling surface that the existing boundary already owns.
    storage.set(getDraftKey("7", 11), "user A's persisted chat draft");
    storage.set("unrelated-key", "survives the identity boundary");

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-b", user: USER_B },
    });

    const { login } = useAuthStore.getState();
    await login("userb", "pw");

    // The boundary really crossed.
    expect(useAuthStore.getState().user?.id).toBe(22);
    // Every canvas draft key is REMOVED (the discriminator: they survive at
    // the pre-fix base).
    expect(window.localStorage.getItem(`${CANVAS_DRAFT_PREFIX}art-1`)).toBeNull();
    expect(window.localStorage.getItem(`${CANVAS_DRAFT_PREFIX}art-2`)).toBeNull();
    const surviving = Array.from(storage.keys());
    expect(surviving.every((k) => !k.startsWith(CANVAS_DRAFT_PREFIX))).toBe(true);
    // The chat-draft namespace is still cleared by the same boundary.
    expect(window.localStorage.getItem(getDraftKey("7", 11))).toBeNull();
    // The clear is scoped, not a storage.clear().
    expect(window.localStorage.getItem("unrelated-key")).toBe(
      "survives the identity boundary"
    );
  });
});
