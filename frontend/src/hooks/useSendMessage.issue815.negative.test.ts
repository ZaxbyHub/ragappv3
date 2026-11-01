// Issue #815 negative parsing pins (unfrozen, plan-critic rounds 1-2):
// the keepalive wrapper must resolve null — never throw, never fall back to
// sequential single-message writes — on non-2xx, invalid JSON, and a 2xx
// malformed envelope, and the hooked tracked chain must never reject into a
// pagehide caller. Mock shape mirrors useSendMessage.issue815.test.ts.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const csrfTokenMock = vi.hoisted(() => vi.fn());
const csrfCookieMock = vi.hoisted(() => vi.fn());
const jwtAccessToken = vi.hoisted(() => ({ value: "jwt-token" as string | null }));

vi.mock("@/lib/api/core", () => ({
  apiClient: { post: vi.fn() },
  API_BASE_URL: "/api",
  get _jwtAccessToken() {
    return jwtAccessToken.value;
  },
  getCsrfCookie: () => csrfCookieMock(),
  getCsrfToken: () => csrfTokenMock(),
}));

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessagesBatch: (...args: unknown[]) => apiMocks.addChatMessagesBatch(...args),
  addChatMessagesBatchKeepalive: (...args: unknown[]) =>
    apiMocks.addChatMessagesBatchKeepalive(...args),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
}));

import { addChatMessagesBatchKeepalive } from "@/lib/api/sessions";

const wrapperMessages = [
  { role: "user" as const, content: "q" },
  { role: "assistant" as const, content: "a" },
];

describe("addChatMessagesBatchKeepalive failure contract (issue #815)", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    csrfTokenMock.mockReturnValue("csrf-token");
    csrfCookieMock.mockReturnValue(null);
    jwtAccessToken.value = "jwt-token";
    fetchMock.mockReset();
    fetchMock.mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("resolves undefined on a non-2xx response (no retry, no throw)", async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 503 });
    await expect(addChatMessagesBatchKeepalive(42, wrapperMessages)).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("resolves undefined when the body is not valid JSON", async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => Promise.reject(new Error("bad body")) });
    await expect(addChatMessagesBatchKeepalive(42, wrapperMessages)).resolves.toBeUndefined();
  });

  it("resolves undefined on a 2xx response whose envelope has no messages array", async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({ unexpected: true }) });
    await expect(addChatMessagesBatchKeepalive(42, wrapperMessages)).resolves.toBeUndefined();
  });

  it("never issues sequential fallback writes (pagehide must not start new requests)", async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 404 });
    await addChatMessagesBatchKeepalive(42, wrapperMessages);
    expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("resolves undefined when fetch itself rejects (transport death)", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("network down"));
    await expect(addChatMessagesBatchKeepalive(42, wrapperMessages)).resolves.toBeUndefined();
  });
});
