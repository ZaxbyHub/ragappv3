import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  HealthResponse,
  ConnectionCheck,
  ConnectionTestResult,
  SettingsResponse,
  UpdateSettingsRequest,
  CuratorTestResult,
  SearchMemoriesRequest,
  MemoryResult,
  AddMemoryRequest,
  AddMemoryResponse,
  SearchMemoriesResponse,
  Tag,
  Document,
  Folder,
  DocumentSortBy,
  SortOrder,
  ListDocumentsOptions,
  ListDocumentsResponse,
  UploadDocumentResponse,
  DocumentStatusResponse,
  DocumentStatsResponse,
  ScanDocumentsResponse,
  ChatMessage,
  Source,
  ChunkContextResponse,
  UsedMemory,
  CitationValidationDebug,
  WikiReference,
  KMSReference,
  ChatStreamCallbacks,
  ChatHistoryItem,
  ChatSession,
  ChatSessionMessage,
  ChatSessionDetail,
  CreateSessionRequest,
  AddMessageRequest,
  Organization,
  Vault,
  VaultListResponse,
  VaultCreateRequest,
  VaultUpdateRequest,
  LlmModeHealth,
  Session,
  SessionListResponse,
  ChangePasswordRequest,
  Group,
  GroupCreateRequest,
  GroupUpdateRequest,
  GroupListResponse,
  GroupMember,
  User,
  UserListItem,
  GroupVault,
  VaultAccessItem,
  VaultGroupAccess,
  VaultEnrichmentToggleRequest,
  UpdateMemoryRequest,
} from "@/lib/api/index";

/**
 * Verification tests for Task 1.1 — api/core.ts and api/index.ts extraction
 *
 * These tests verify that the extraction of shared infrastructure, types, and
 * non-domain functions from api.ts into api/core.ts (with api/index.ts barrel)
 * is functionally correct and backward-compatible with existing consumers.
 *
 * PURE CODE EXTRACTION — no new logic, no behavior changed.
 *
 * NOTE: The original api.ts has been deleted. The barrel at api/index.ts
 * inherits the @/lib/api import path via directory module resolution.
 * (moduleResolution: "bundler" resolves lib/api.ts → not found,
 * then lib/api/index.ts → found.)
 */

describe("api/core.ts — module import", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("should import core.ts without errors", async () => {
    // Verify the core module can be imported directly
    const core = await import("@/lib/api/core");
    expect(core).toBeDefined();
    expect(typeof core.API_BASE_URL).toBe("string");
  });

  it("should export API_BASE_URL from core.ts", async () => {
    const { API_BASE_URL } = await import("@/lib/api/core");
    expect(API_BASE_URL).toBe("/api");
  });

  it("should export auth infrastructure functions from core.ts", async () => {
    const {
      setJwtAccessToken,
      getJwtAccessToken,
      resetCsrfToken,
      getCsrfToken,
      ensureCsrfToken,
      attachCsrfInterceptor,
      loginRedirectPath,
      redirectToLogin,
      refreshAccessToken,
    } = await import("@/lib/api/core");

    expect(typeof setJwtAccessToken).toBe("function");
    expect(typeof getJwtAccessToken).toBe("function");
    expect(typeof resetCsrfToken).toBe("function");
    expect(typeof getCsrfToken).toBe("function");
    expect(typeof ensureCsrfToken).toBe("function"); // async functions typeof as 'function'
    expect(typeof attachCsrfInterceptor).toBe("function");
    expect(typeof loginRedirectPath).toBe("function");
    expect(typeof redirectToLogin).toBe("function");
    expect(typeof refreshAccessToken).toBe("function");
  });

  it("should export getTokenExpiry and isTokenNearExpiry (previously private)", async () => {
    // These were extracted from api.ts where they were module-private
    const { getTokenExpiry, isTokenNearExpiry } = await import("@/lib/api/core");

    expect(typeof getTokenExpiry).toBe("function");
    expect(typeof isTokenNearExpiry).toBe("function");

    // Verify correct JWT parsing
    // JWT: header.payload.signature (exp is in payload)
    // Test with a known exp value
    const payload = { exp: Math.floor(Date.now() / 1000) + 3600 }; // 1 hour from now
    const base64Payload = btoa(JSON.stringify(payload));
    const testToken = `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.${base64Payload}.signature`;

    const expiry = getTokenExpiry(testToken);
    expect(expiry).toBe(payload.exp * 1000);
  });

  it("should export transient retry utilities from core.ts", async () => {
    const { isTransientRetryableRequest, transientRetryDelayMs } = await import(
      "@/lib/api/core"
    );

    expect(typeof isTransientRetryableRequest).toBe("function");
    expect(typeof transientRetryDelayMs).toBe("function");

    // Verify transientRetryDelayMs behavior matches api.test.ts expectations
    expect(transientRetryDelayMs(0)).toBe(300);
    expect(transientRetryDelayMs(1)).toBe(900);
    expect(transientRetryDelayMs(99)).toBe(900);
  });

  it("should export apiClient as default from core.ts", async () => {
    const apiClientModule = await import("@/lib/api/core");
    expect(apiClientModule.default).toBeDefined();
    // apiClient is an axios instance - typeof returns 'object' but it's a function constructor
    expect(apiClientModule.default).toHaveProperty("get");
    expect(apiClientModule.default).toHaveProperty("post");
  });
});

describe("api/index.ts — barrel re-exports (direct import)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("should re-export API_BASE_URL via barrel (direct index.ts import)", async () => {
    // Import directly from the index.ts barrel
    const indexModule = await import("@/lib/api/index");
    expect(typeof indexModule.API_BASE_URL).toBe("string");
    expect(indexModule.API_BASE_URL).toBe("/api");
  });

  it("should re-export auth infrastructure via barrel", async () => {
    const {
      setJwtAccessToken,
      getJwtAccessToken,
      resetCsrfToken,
      getCsrfToken,
      ensureCsrfToken,
      attachCsrfInterceptor,
      loginRedirectPath,
      redirectToLogin,
      refreshAccessToken,
      isTransientRetryableRequest,
      transientRetryDelayMs,
    } = await import("@/lib/api/index");

    expect(typeof setJwtAccessToken).toBe("function");
    expect(typeof getJwtAccessToken).toBe("function");
    expect(typeof resetCsrfToken).toBe("function");
    expect(typeof getCsrfToken).toBe("function");
    expect(typeof ensureCsrfToken).toBe("function");
    expect(typeof attachCsrfInterceptor).toBe("function");
    expect(typeof loginRedirectPath).toBe("function");
    expect(typeof redirectToLogin).toBe("function");
    expect(typeof refreshAccessToken).toBe("function");
    expect(typeof isTransientRetryableRequest).toBe("function");
    expect(typeof transientRetryDelayMs).toBe("function");
  });

  it("should re-export getTokenExpiry and isTokenNearExpiry via barrel", async () => {
    const { getTokenExpiry, isTokenNearExpiry } = await import("@/lib/api/index");

    expect(typeof getTokenExpiry).toBe("function");
    expect(typeof isTokenNearExpiry).toBe("function");

    // Functional verification: near-expiry check
    const soonPayload = { exp: Math.floor(Date.now() / 1000) + 30 }; // 30 seconds from now
    const soonToken = `header.${btoa(JSON.stringify(soonPayload))}.sig`;
    expect(isTokenNearExpiry(soonToken, 60000)).toBe(true); // within 1 minute buffer

    const farPayload = { exp: Math.floor(Date.now() / 1000) + 7200 }; // 2 hours from now
    const farToken = `header.${btoa(JSON.stringify(farPayload))}.sig`;
    expect(isTokenNearExpiry(farToken, 60000)).toBe(false); // not near expiry
  });

  it("should export apiClient as default via barrel", async () => {
    const indexModule = await import("@/lib/api/index");
    expect(indexModule.default).toBeDefined();
    expect(indexModule.default).toHaveProperty("get");
    expect(indexModule.default).toHaveProperty("post");
  });

  it("should re-export all shared types via barrel (compile-time verification)", () => {
    // This test verifies that all type exports compile correctly.
    // If any type is missing from the barrel re-export, TypeScript will error here.
    // We use type annotations to trigger compile-time verification without runtime values.
    // The types themselves are compile-time only - TypeScript erases them at runtime.
    const _verifyTypes = <T>() => {
      // This generic function is never called at runtime but TypeScript validates
      // all type usages inside it at compile time.
    };

    // Verify key types are usable in type annotations
    // If these compile, the types are properly exported
    const _health: HealthResponse = {} as HealthResponse;
    const _connection: ConnectionCheck = {} as ConnectionCheck;
    const _settings: SettingsResponse = {} as SettingsResponse;
    const _vault: Vault = {} as Vault;
    const _session: ChatSession = {} as ChatSession;
    const _document: Document = {} as Document;
    const _group: Group = {} as Group;
    const _user: User = {} as User;

    void _verifyTypes;
    void _health;
    void _connection;
    void _settings;
    void _vault;
    void _session;
    void _document;
    void _group;
    void _user;
  });

  it("should re-export all API functions via barrel", async () => {
    const {
      getHealth,
      getLlmModeHealth,
      getSettings,
      testCuratorConnection,
      updateSettings,
      testConnections,
      listOrganizations,
      listVaults,
      listAccessibleVaults,
      getVault,
      createVault,
      updateVault,
      deleteVault,
      toggleVaultEnrichment,
      searchMemories,
      addMemory,
      deleteMemory,
      updateMemory,
      listMemories,
      listDocuments,
      getDocument,
      uploadDocument,
      scanDocuments,
      getDocumentStatus,
      getDocumentRawBlob,
      deleteDocument,
      deleteDocuments,
      deleteAllDocumentsInVault,
      getDocumentStats,
      getChunkContext,
      changePassword,
      listSessions,
      revokeSession,
      revokeAllSessions,
    } = await import("@/lib/api/index");

    expect(typeof getHealth).toBe("function");
    expect(typeof getLlmModeHealth).toBe("function");
    expect(typeof getSettings).toBe("function");
    expect(typeof testCuratorConnection).toBe("function");
    expect(typeof updateSettings).toBe("function");
    expect(typeof testConnections).toBe("function");
    expect(typeof listOrganizations).toBe("function");
    expect(typeof listVaults).toBe("function");
    expect(typeof listAccessibleVaults).toBe("function");
    expect(typeof getVault).toBe("function");
    expect(typeof createVault).toBe("function");
    expect(typeof updateVault).toBe("function");
    expect(typeof deleteVault).toBe("function");
    expect(typeof toggleVaultEnrichment).toBe("function");
    expect(typeof searchMemories).toBe("function");
    expect(typeof addMemory).toBe("function");
    expect(typeof deleteMemory).toBe("function");
    expect(typeof updateMemory).toBe("function");
    expect(typeof listMemories).toBe("function");
    expect(typeof listDocuments).toBe("function");
    expect(typeof getDocument).toBe("function");
    expect(typeof uploadDocument).toBe("function");
    expect(typeof scanDocuments).toBe("function");
    expect(typeof getDocumentStatus).toBe("function");
    expect(typeof getDocumentRawBlob).toBe("function");
    expect(typeof deleteDocument).toBe("function");
    expect(typeof deleteDocuments).toBe("function");
    expect(typeof deleteAllDocumentsInVault).toBe("function");
    expect(typeof getDocumentStats).toBe("function");
    expect(typeof getChunkContext).toBe("function");
    expect(typeof changePassword).toBe("function");
    expect(typeof listSessions).toBe("function");
    expect(typeof revokeSession).toBe("function");
    expect(typeof revokeAllSessions).toBe("function");
  });

  it("should use correct relative import paths in core.ts (../storage, ../paths)", async () => {
    // Verify the paths module can be imported from core.ts location
    vi.stubEnv("VITE_API_URL", "");
    vi.stubEnv("VITE_APP_BASENAME", "");
    vi.resetModules();

    const { API_BASE_URL, loginRedirectPath } = await import("@/lib/api/index");

    // With empty env vars, API_BASE_URL should be derived from appPath("/api")
    expect(API_BASE_URL).toBe("/api");
    expect(loginRedirectPath()).toBe("/login");
  });
});

describe("api/core.ts — backward compatibility with api.ts", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("should produce identical API_BASE_URL with empty env vars", async () => {
    vi.stubEnv("VITE_API_URL", "");
    vi.stubEnv("VITE_APP_BASENAME", "");
    vi.resetModules();

    const { API_BASE_URL, loginRedirectPath } = await import("@/lib/api/core");

    expect(API_BASE_URL).toBe("/api");
    expect(loginRedirectPath()).toBe("/login");
  });

  it("should produce identical API_BASE_URL with prefixed env vars", async () => {
    vi.stubEnv("VITE_API_URL", "/knowledgevault/api");
    vi.stubEnv("VITE_APP_BASENAME", "/knowledgevault");
    vi.resetModules();

    const { API_BASE_URL, loginRedirectPath } = await import("@/lib/api/core");

    expect(API_BASE_URL).toBe("/knowledgevault/api");
    expect(loginRedirectPath()).toBe("/knowledgevault/login");
  });

  it("should produce identical transient retry behavior", async () => {
    const { isTransientRetryableRequest, transientRetryDelayMs } = await import(
      "@/lib/api/core"
    );

    // Identical assertions to api.test.ts
    expect(isTransientRetryableRequest("get", undefined, false)).toBe(true);
    expect(isTransientRetryableRequest("GET", 503, true)).toBe(true);
    expect(isTransientRetryableRequest("head", 504, true)).toBe(true);
    expect(isTransientRetryableRequest("post", 503, true)).toBe(false);
    expect(isTransientRetryableRequest("delete", undefined, false)).toBe(false);
    expect(isTransientRetryableRequest("get", 500, true)).toBe(false);
    expect(isTransientRetryableRequest("get", 401, true)).toBe(false);

    expect(transientRetryDelayMs(0)).toBe(300);
    expect(transientRetryDelayMs(1)).toBe(900);
    expect(transientRetryDelayMs(99)).toBe(900);
  });
});

describe("getTokenExpiry / isTokenNearExpiry — newly exported functions", () => {
  afterEach(() => {
    vi.resetModules();
  });

  it("should parse JWT expiry from token", async () => {
    const { getTokenExpiry } = await import("@/lib/api/core");

    // Create a JWT with known expiry
    const exp = Math.floor(Date.now() / 1000) + 3600; // 1 hour from now
    const payload = { exp };
    const base64Payload = btoa(JSON.stringify(payload));
    const testToken = `header.${base64Payload}.signature`;

    const expiry = getTokenExpiry(testToken);
    expect(expiry).toBe(exp * 1000);
  });

  it("should return null for invalid token format", async () => {
    const { getTokenExpiry } = await import("@/lib/api/core");

    expect(getTokenExpiry("not-a-jwt")).toBeNull();
    expect(getTokenExpiry("only.twoparts")).toBeNull();
    expect(getTokenExpiry("")).toBeNull();
  });

  it("should detect near-expiry tokens", async () => {
    const { isTokenNearExpiry } = await import("@/lib/api/core");

    // Token expiring in 30 seconds - within 60s buffer
    const soonExp = Math.floor(Date.now() / 1000) + 30;
    const soonPayload = { exp: soonExp };
    const soonToken = `header.${btoa(JSON.stringify(soonPayload))}.sig`;
    expect(isTokenNearExpiry(soonToken, 60000)).toBe(true);

    // Token expiring in 2 hours - outside 60s buffer
    const farExp = Math.floor(Date.now() / 1000) + 7200;
    const farPayload = { exp: farExp };
    const farToken = `header.${btoa(JSON.stringify(farPayload))}.sig`;
    expect(isTokenNearExpiry(farToken, 60000)).toBe(false);
  });

  it("should return false for invalid tokens in isTokenNearExpiry", async () => {
    const { isTokenNearExpiry } = await import("@/lib/api/core");

    expect(isTokenNearExpiry("invalid-token")).toBe(false);
    expect(isTokenNearExpiry("")).toBe(false);
  });
});
