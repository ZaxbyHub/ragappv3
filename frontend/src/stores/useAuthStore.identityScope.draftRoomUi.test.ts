// Issue #1032 C5: an identity switch must clear useDraftRoomUiStore.draftText.
// The store holds unsaved Draft Room editor text keyed per draft id
// (useDraftRoomUiStore.ts, draftText: Record<number, string>) at module
// level, and resetForDraft deliberately EXCLUDES draftText from its reset —
// nothing at the credential boundaries clears it, so user A's in-progress
// manuscript text is rehydrated into user B's editor after a
// different-principal login in the same tab.
//
// Boundary discrimination is pinned by the second leg: a SAME-principal
// re-login must NOT clear (that leg passes at the pre-fix base and must keep
// passing — the fix clears at identity boundaries, not at every login).
//
// Transport mocking mirrors useAuthStore.identitySwitch.issue823.test.ts;
// the auth store and the Draft Room UI store under test are REAL.

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
import { useDraftRoomUiStore } from "./useDraftRoomUiStore";

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

describe("useAuthStore identity scope — Draft Room UI draftText (issue #1032)", () => {
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
    // The UI store is module-level; reset the per-draft editor text between
    // tests so each leg seeds exactly what it asserts on.
    useDraftRoomUiStore.setState({ draftText: {} });
    installLocalStorage();
  });

  afterEach(() => {
    resetInitState();
    useDraftRoomUiStore.setState({ draftText: {} });
  });

  it("different-principal login clears useDraftRoomUiStore.draftText", async () => {
    useAuthStore.setState({
      user: USER_A,
      accessToken: "jwt-a",
      isAuthenticated: true,
      authMode: "jwt",
    });
    useDraftRoomUiStore.getState().setDraftText(7, "user A's text");
    useDraftRoomUiStore.getState().setDraftText(8, "more A text");
    expect(Object.keys(useDraftRoomUiStore.getState().draftText)).toHaveLength(2);

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-b", user: USER_B },
    });

    const { login } = useAuthStore.getState();
    await login("userb", "pw");

    // The boundary really crossed.
    expect(useAuthStore.getState().user?.id).toBe(22);
    expect(useDraftRoomUiStore.getState().draftText).toEqual({});
  });

  it("same-principal re-login does not clear draftText (boundary discrimination)", async () => {
    useAuthStore.setState({
      user: USER_A,
      accessToken: "jwt-a",
      isAuthenticated: true,
      authMode: "jwt",
    });
    useDraftRoomUiStore.getState().setDraftText(7, "user A's text");
    useDraftRoomUiStore.getState().setDraftText(8, "more A text");

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-a2", user: USER_A },
    });

    await useAuthStore.getState().login("usera", "pw");

    expect(useAuthStore.getState().user?.id).toBe(11);
    expect(useDraftRoomUiStore.getState().draftText).toEqual({
      7: "user A's text",
      8: "more A text",
    });
  });
});
