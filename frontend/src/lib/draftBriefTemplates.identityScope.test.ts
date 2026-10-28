// Issue #1036 C2 (collated with #1037): an identity switch must keep the
// next principal from reading the previous principal's Draft Room brief
// templates. FINAL design (plan-critic round 2): templates live under
// PRINCIPAL-SCOPED keys — prefix `ragapp.v1.draft-brief-templates:` +
// principalId — with a per-principal module cache; the identity boundary
// clears the prefix namespace and removes the legacy GLOBAL key
// best-effort. The legacy payload is UNOWNED: it is never migrated into
// any principal's namespace (migrating it could hand one principal's
// templates to another).
//
// Every lib call passes an explicit principal id: `saveDraftBriefTemplate(
// name, brief, principalId)` / `listDraftBriefTemplates(principalId)`. At
// the pre-fix base the extra argument is simply ignored and every call
// backs onto the shared global key — that IS the defect surface under
// test, and it is what keeps each base leg RED/GREEN meaningful.
//
// Storage assertions are PREFIX-level: localStorage keys are enumerated
// against the template key prefix (the fixed lib exports the prefix
// constant; at base it does not exist yet, so the prefix is derived from
// the exported legacy constant plus ":" — never a hardcoded principal-
// scoped key name). No migration is asserted anywhere.
//
// Legs:
//   (i)   different-principal login on healthy storage: B's list is empty,
//         no prefix key holds A's payload, and no key OUTSIDE the prefix
//         holds a template payload (the legacy global key is gone) —
//         RED at base.
//   (ii)  same-principal re-login on healthy storage retains A's
//         templates — passes at base, must keep passing.
//   (iii) different-principal login on degraded storage (getItem works,
//         removeItem throws for the template namespace): B's list is empty
//         — principal scoping means B never reads A's key — RED at base.
//   (iv)  same-principal re-login on degraded storage retains — passes at
//         base, must keep passing.
//   (v)   legacy residue: the legacy GLOBAL key payload is seeded directly
//         via setItem; after a different-principal login the legacy payload
//         must NEVER appear under B's list even though its (best-effort)
//         removal fails on degraded storage — RED at base (base lists the
//         global payload for everyone).
//
// Transport mocking mirrors useAuthStore.identityScope.draftRoomUi.test.ts;
// the auth store and the template module under test are REAL.

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
  listAccessibleVaults: vi.fn().mockResolvedValue({ vaults: [] }),
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

import { useAuthStore, resetInitState } from "@/stores/useAuthStore";
import * as templatesModule from "@/lib/draftBriefTemplates";
import {
  DRAFT_BRIEF_TEMPLATES_STORAGE_KEY,
  resetDraftBriefTemplatesCacheForTests,
} from "@/lib/draftBriefTemplates";
import type { DraftBrief } from "@/lib/api/draftRoom";

// The fixed lib's principal-scoped call surface. Typed through a local
// handle so the file compiles at BOTH ends: at base the extra principalId
// argument is ignored by the 2-argument implementation (JS truth), at the
// fixed head it selects the principal-scoped namespace.
type SavedTemplate = { id: string; name: string; brief: DraftBrief };
type PrincipalScopedTemplateLib = {
  saveDraftBriefTemplate: (
    name: string,
    brief: DraftBrief,
    principalId: number
  ) => SavedTemplate[];
  listDraftBriefTemplates: (principalId: number) => SavedTemplate[];
  DRAFT_BRIEF_TEMPLATES_KEY_PREFIX?: string;
};

const templatesLib = templatesModule as unknown as PrincipalScopedTemplateLib;

// The template key prefix: the fixed lib exports it; at base only the
// legacy global key constant exists, and the final prefix is that constant
// plus ":" (derived from the export — never a hardcoded future key name).
const TEMPLATE_KEY_PREFIX: string =
  templatesLib.DRAFT_BRIEF_TEMPLATES_KEY_PREFIX ??
  `${DRAFT_BRIEF_TEMPLATES_STORAGE_KEY}:`;

const USER_A = { id: 11, username: "usera", full_name: "User A", role: "admin" as const, is_active: true };
const USER_B = { id: 22, username: "userb", full_name: "User B", role: "admin" as const, is_active: true };

const USER_A_BRIEF: DraftBrief = {
  piece_type: "memo",
  audience: "User A's private audience",
  purpose: "persuade",
  tone: "formal",
  target_words: 800,
  transformation_strength: "moderate",
  primary_input_id: null,
  must_include: ["user A's must-include phrase"],
  must_avoid: ["user A's must-avoid phrase"],
  preserve_quotes: true,
  preserve_numbers: true,
  preserve_uncertainty: false,
  drafting_priority: "accuracy",
  additional_instructions: "user A's private instructions",
};

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

/**
 * Degraded storage over the SAME backing map (getItem still returns stored
 * values, setItem still works) whose removeItem THROWS for the template
 * namespace (the legacy global key and the principal-scoped prefix) — a
 * best-effort removal fails on this surface, so only principal scoping
 * (the next principal never reads the previous principal's key) keeps the
 * templates private. Scoped by the lib's exported constants, never by a
 * raw future key name.
 */
function installDegradedTemplateStorage(store: Map<string, string>) {
  const isTemplateKey = (k: string) =>
    k === DRAFT_BRIEF_TEMPLATES_STORAGE_KEY || k.startsWith(TEMPLATE_KEY_PREFIX);
  const impl = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => {
      if (isTemplateKey(k)) {
        throw new DOMException("degraded storage: removeItem failed", "QuotaExceededError");
      }
      store.delete(k);
    },
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(window, "localStorage", { value: impl, configurable: true });
}

function looksLikeTemplateEntry(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { name?: unknown }).name === "string" &&
    "brief" in value
  );
}

/** True when raw storage contents parse as an array with template entries. */
function holdsTemplatePayload(raw: string | null): boolean {
  if (raw === null) return false;
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) && parsed.some(looksLikeTemplateEntry);
  } catch {
    return false;
  }
}

function localStorageKeys(): string[] {
  const keys: string[] = [];
  for (let i = 0; i < window.localStorage.length; i += 1) {
    const key = window.localStorage.key(i);
    if (key !== null) keys.push(key);
  }
  return keys;
}

/** Keys under the principal-scoped template prefix holding template payloads. */
function prefixKeysHoldingTemplates(): string[] {
  return localStorageKeys()
    .filter((k) => k.startsWith(TEMPLATE_KEY_PREFIX))
    .filter((k) => holdsTemplatePayload(window.localStorage.getItem(k)));
}

/** Keys OUTSIDE the prefix (e.g. the legacy global key) holding payloads. */
function keysOutsidePrefixHoldingTemplates(): string[] {
  return localStorageKeys()
    .filter((k) => !k.startsWith(TEMPLATE_KEY_PREFIX))
    .filter((k) => holdsTemplatePayload(window.localStorage.getItem(k)));
}

describe("identity scope — principal-scoped Draft Room brief templates (issue #1036 C2)", () => {
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
    // The template module's session cache is module-level; reset it so each
    // leg seeds exactly what it asserts on.
    resetDraftBriefTemplatesCacheForTests();
    installLocalStorage();
  });

  afterEach(() => {
    resetInitState();
    resetDraftBriefTemplatesCacheForTests();
  });

  it("(i) different-principal login clears A's templates for B (list, prefix, legacy key)", async () => {
    useAuthStore.setState({
      user: USER_A,
      accessToken: "jwt-a",
      isAuthenticated: true,
      authMode: "jwt",
    });
    templatesLib.saveDraftBriefTemplate("User A's template", USER_A_BRIEF, USER_A.id);
    expect(templatesLib.listDraftBriefTemplates(USER_A.id)).toHaveLength(1);

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-b", user: USER_B },
    });

    const { login } = useAuthStore.getState();
    await login("userb", "pw");

    // The boundary really crossed.
    expect(useAuthStore.getState().user?.id).toBe(22);

    // B's principal-scoped list must be empty.
    expect(templatesLib.listDraftBriefTemplates(USER_B.id)).toEqual([]);

    // No principal-scoped key may still hold A's payload after the boundary.
    expect(prefixKeysHoldingTemplates()).toEqual([]);

    // No key OUTSIDE the prefix may hold a template payload: on healthy
    // storage the legacy global key must be removed at the boundary (its
    // payload is unowned and must never surface for another principal).
    expect(keysOutsidePrefixHoldingTemplates()).toEqual([]);

    // No stale cache either: a forced storage re-read still sees none for B.
    resetDraftBriefTemplatesCacheForTests();
    expect(templatesLib.listDraftBriefTemplates(USER_B.id)).toEqual([]);
  });

  it("(ii) same-principal re-login retains A's templates (boundary discrimination)", async () => {
    useAuthStore.setState({
      user: USER_A,
      accessToken: "jwt-a",
      isAuthenticated: true,
      authMode: "jwt",
    });
    templatesLib.saveDraftBriefTemplate("User A's template", USER_A_BRIEF, USER_A.id);
    expect(templatesLib.listDraftBriefTemplates(USER_A.id)).toHaveLength(1);

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-a2", user: USER_A },
    });

    await useAuthStore.getState().login("usera", "pw");

    expect(useAuthStore.getState().user?.id).toBe(11);
    const templates = templatesLib.listDraftBriefTemplates(USER_A.id);
    expect(templates).toHaveLength(1);
    expect(templates[0].name).toBe("User A's template");
  });

  it("(iii) different-principal login on degraded storage does not list A's templates (principal scoping)", async () => {
    useAuthStore.setState({
      user: USER_A,
      accessToken: "jwt-a",
      isAuthenticated: true,
      authMode: "jwt",
    });
    const store = installLocalStorage();
    templatesLib.saveDraftBriefTemplate("User A's private template", USER_A_BRIEF, USER_A.id);
    expect(templatesLib.listDraftBriefTemplates(USER_A.id)).toHaveLength(1);
    installDegradedTemplateStorage(store);

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-b", user: USER_B },
    });

    await useAuthStore.getState().login("userb", "pw");

    expect(useAuthStore.getState().user?.id).toBe(22);
    // removeItem failed for the template namespace — so only principal
    // scoping (B never reads A's key) can produce an empty list here.
    expect(templatesLib.listDraftBriefTemplates(USER_B.id)).toEqual([]);
    resetDraftBriefTemplatesCacheForTests();
    expect(templatesLib.listDraftBriefTemplates(USER_B.id)).toEqual([]);
  });

  it("(iv) same-principal re-login on degraded storage retains templates", async () => {
    useAuthStore.setState({
      user: USER_A,
      accessToken: "jwt-a",
      isAuthenticated: true,
      authMode: "jwt",
    });
    const store = installLocalStorage();
    templatesLib.saveDraftBriefTemplate("User A's private template", USER_A_BRIEF, USER_A.id);
    expect(templatesLib.listDraftBriefTemplates(USER_A.id)).toHaveLength(1);
    installDegradedTemplateStorage(store);

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-a2", user: USER_A },
    });

    await useAuthStore.getState().login("usera", "pw");

    expect(useAuthStore.getState().user?.id).toBe(11);
    expect(templatesLib.listDraftBriefTemplates(USER_A.id)).toHaveLength(1);
    expect(templatesLib.listDraftBriefTemplates(USER_A.id)[0].name).toBe(
      "User A's private template"
    );
  });

  it("(v) legacy global-key residue never surfaces under the next principal (no migration)", async () => {
    useAuthStore.setState({
      user: USER_A,
      accessToken: "jwt-a",
      isAuthenticated: true,
      authMode: "jwt",
    });
    const store = installLocalStorage();
    // Seed the legacy GLOBAL key payload directly — the unowned residue a
    // pre-scoping session leaves behind.
    window.localStorage.setItem(
      DRAFT_BRIEF_TEMPLATES_STORAGE_KEY,
      JSON.stringify([
        { id: "legacy-1", name: "Unowned legacy template", brief: USER_A_BRIEF },
      ])
    );
    resetDraftBriefTemplatesCacheForTests();
    installDegradedTemplateStorage(store);

    mockPostFn.mockResolvedValueOnce({
      data: { access_token: "jwt-b", user: USER_B },
    });

    await useAuthStore.getState().login("userb", "pw");

    expect(useAuthStore.getState().user?.id).toBe(22);
    // The legacy payload is unowned: even though its best-effort removal
    // FAILED (degraded storage — the key is still present), it must never
    // be migrated into or read under B's namespace.
    expect(window.localStorage.getItem(DRAFT_BRIEF_TEMPLATES_STORAGE_KEY)).not.toBeNull();
    expect(templatesLib.listDraftBriefTemplates(USER_B.id)).toEqual([]);
    resetDraftBriefTemplatesCacheForTests();
    expect(templatesLib.listDraftBriefTemplates(USER_B.id)).toEqual([]);
  });
});
