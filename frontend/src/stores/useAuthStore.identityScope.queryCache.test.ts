// Issue #1032 C3: an identity switch must clear the app-level React Query
// cache. main.tsx constructs the QueryClient as a module-level singleton and
// never clears it at the credential boundaries, and the Draft Room / Canvas
// query keys (canvasKeys.*, draftRoomKeys*) carry no principal segment — so
// user A's cached canvas/draft-room payloads render for user B after a
// different-principal login in the same tab.
//
// ACCESS-PATH ASSUMPTION (stated, plan-critic-reviewed): the fix exposes the
// app-level QueryClient through `@/lib/queryClient` (main.tsx then renders
// with that same singleton). That module does NOT exist at the pre-fix base
// — main.tsx builds the client inline — so this import fails and the suite
// is RED at base by construction. If the fix adopts a different export path,
// this import (and only this import) must be updated to match it.
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

import { queryClient } from "@/lib/queryClient";
import { useAuthStore, resetInitState } from "./useAuthStore";

const USER_A = { id: 11, username: "usera", full_name: "User A", role: "admin" as const, is_active: true };
const USER_B = { id: 22, username: "userb", full_name: "User B", role: "admin" as const, is_active: true };

// Key shapes mirror the real factories: canvasKeys.artifact(uid) =>
// ["canvas", "artifact", uid] (src/lib/api/canvas.ts) and
// draftRoomKeys.lists() => ["draft-room", "drafts"] (src/lib/api/draftRoom.ts)
// — neither carries a principal segment, which is exactly why the cached
// payloads cross the identity boundary at base.
const CANVAS_ARTIFACT_KEY = ["canvas", "artifact", "art-1"] as const;
const DRAFT_ROOM_LIST_KEY = ["draft-room", "drafts"] as const;

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

function seedUserAPrincipal() {
  useAuthStore.setState({
    user: USER_A,
    accessToken: "jwt-a",
    isAuthenticated: true,
    authMode: "jwt",
  });
}

describe("useAuthStore identity scope — app-level query cache (issue #1032)", () => {
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
    queryClient.clear();
    installLocalStorage();
  });

  afterEach(() => {
    resetInitState();
    queryClient.clear();
  });

  it("different-principal login clears user A's cached canvas/draft-room queries", async () => {
    seedUserAPrincipal();
    queryClient.setQueryData(CANVAS_ARTIFACT_KEY, {
      content: "user A's canvas markdown",
      updated_at: "2026-01-01T00:00:00Z",
    });
    queryClient.setQueryData(DRAFT_ROOM_LIST_KEY, [
      { id: 1, title: "user A's private draft" },
    ]);
    expect(queryClient.getQueryData(CANVAS_ARTIFACT_KEY)).toBeDefined();
    expect(queryClient.getQueryData(DRAFT_ROOM_LIST_KEY)).toBeDefined();

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-b", user: USER_B },
    });

    const { login } = useAuthStore.getState();
    await login("userb", "pw");

    // The boundary really crossed.
    expect(useAuthStore.getState().user?.id).toBe(22);
    // User A's cached payloads are gone from the app-level singleton.
    expect(queryClient.getQueryData(CANVAS_ARTIFACT_KEY)).toBeUndefined();
    expect(queryClient.getQueryData(DRAFT_ROOM_LIST_KEY)).toBeUndefined();
    expect(queryClient.getQueryCache().getAll()).toEqual([]);
  });

  it("same-principal re-login does not clear the query cache (boundary discrimination)", async () => {
    seedUserAPrincipal();
    queryClient.setQueryData(CANVAS_ARTIFACT_KEY, {
      content: "user A's canvas markdown",
    });

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-a2", user: USER_A },
    });

    await useAuthStore.getState().login("usera", "pw");

    expect(useAuthStore.getState().user?.id).toBe(11);
    expect(queryClient.getQueryData(CANVAS_ARTIFACT_KEY)).toBeDefined();
  });

  it("an in-flight query settling after the boundary does not repopulate the cleared cache", async () => {
    seedUserAPrincipal();
    let resolveFetch: (v: { secret: string }) => void = () => undefined;
    const heldFetch = new Promise<{ secret: string }>((resolve) => {
      resolveFetch = resolve;
    });
    const inflight = queryClient.fetchQuery({
      queryKey: ["canvas", "inflight", "user-a"],
      queryFn: () => heldFetch,
    });
    // Attach the rejection handler IMMEDIATELY: the boundary's clear()
    // cancels this fetch, and a promise rejected before any handler is
    // attached surfaces as an unhandled rejection and can fail the run
    // (plan-critic round 2, finding 7).
    const inflightSettled = inflight.catch(() => undefined);
    // Let the fetch actually start before the boundary crosses.
    await Promise.resolve();

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-b", user: USER_B },
    });
    await useAuthStore.getState().login("userb", "pw");
    expect(useAuthStore.getState().user?.id).toBe(22);

    // The old principal's transport resolves only AFTER the switch cleared
    // the cache — its payload must not re-enter the cache.
    resolveFetch({ secret: "user A's late payload" });
    await inflightSettled;
    await new Promise((r) => setTimeout(r, 0));

    expect(
      queryClient.getQueryData(["canvas", "inflight", "user-a"]),
    ).toBeUndefined();
    expect(
      queryClient
        .getQueryCache()
        .getAll()
        .some(
          (q) => Array.isArray(q.queryKey) && q.queryKey[0] === "canvas",
        ),
    ).toBe(false);
  });
});
