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

import { addChatMessagesBatchKeepalive } from "./sessions";

const messages = [
  { role: "user", content: "question", turn_id: "turn-552" },
  { role: "assistant", content: "partial", turn_id: "turn-552", status: "interrupted" },
];

describe("addChatMessagesBatchKeepalive (issue #552)", () => {
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

  it("sends one exact credentialed keepalive batch request", async () => {
    await addChatMessagesBatchKeepalive(42, messages);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/chat/sessions/42/messages/batch",
      {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": "csrf-token",
          Authorization: "Bearer jwt-token",
        },
        body: JSON.stringify({ messages }),
        keepalive: true,
      },
    );
  });

  it("fails closed without sending when no cached CSRF token exists", async () => {
    csrfTokenMock.mockReturnValue(null);

    await addChatMessagesBatchKeepalive(42, messages);

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses the readable CSRF cookie after the in-memory token was cleared", async () => {
    csrfTokenMock.mockReturnValue(null);
    csrfCookieMock.mockReturnValue("cookie-token");

    await addChatMessagesBatchKeepalive(42, messages);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/chat/sessions/42/messages/batch",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-CSRF-Token": "cookie-token" }),
      }),
    );
  });

  it("omits the Authorization header when no JWT access token is cached", async () => {
    jwtAccessToken.value = null;

    await addChatMessagesBatchKeepalive(42, messages);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, request] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(request.headers).toEqual({
      "Content-Type": "application/json",
      "X-CSRF-Token": "csrf-token",
    });
  });

  it("skips an oversized keepalive body before exceeding the browser quota", async () => {
    const oversizedMessages = [
      { role: "user", content: "x".repeat(70_000), turn_id: "turn-552" },
    ];

    await addChatMessagesBatchKeepalive(42, oversizedMessages);

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("swallows transport rejection because pagehide has no retry window", async () => {
    fetchMock.mockRejectedValueOnce(new Error("document is unloading"));

    await expect(addChatMessagesBatchKeepalive(42, messages)).resolves.toBeUndefined();
  });
});
