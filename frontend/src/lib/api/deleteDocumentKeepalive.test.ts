// deleteDocumentKeepalive (issue #766) — the pagehide flush channel for a
// confirmed document delete.
//
// The function under test lives INSIDE ./core, so the sessions.keepalive.test
// trick (replacing the "./core" module so its accessors mock out) cannot
// apply here: intra-module calls resolve to the local bindings and never see
// a partial-mock override. Instead these tests import the REAL core and
// drive it through its real state levers — the readable CSRF cookie via
// document.cookie (the same seeding pattern as core.transport-owner.issue775
// .test.ts) and the exported setJwtAccessToken for the bearer — stubbing
// only the global fetch the channel rides.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { deleteDocumentKeepalive, setJwtAccessToken } from "./core";

/** Drain the promise chains the keepalive outcome logging rides on. */
async function flushMicrotasks(rounds = 10): Promise<void> {
  for (let i = 0; i < rounds; i += 1) await Promise.resolve();
}

describe("deleteDocumentKeepalive (issue #766)", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockResolvedValue({ ok: true } as Response);
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "error").mockImplementation(() => undefined);
  });

  afterEach(() => {
    document.cookie = "X-CSRF-Token=; path=/; max-age=0";
    setJwtAccessToken(null);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("sends one exact credentialed keepalive delete when a credential is cached", async () => {
    document.cookie = "X-CSRF-Token=tok";
    setJwtAccessToken("jwt-token");

    const attempted = deleteDocumentKeepalive("7");

    expect(attempted).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/documents\/7$/),
      {
        method: "DELETE",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": "tok",
          Authorization: "Bearer jwt-token",
        },
        keepalive: true,
      },
    );
    await flushMicrotasks();
    expect(console.error).not.toHaveBeenCalled();
  });

  it("logs a non-2xx outcome instead of dropping it silently, and never throws", async () => {
    document.cookie = "X-CSRF-Token=tok";
    fetchMock.mockResolvedValue({ ok: false, status: 403 } as Response);
    const errorSpy = vi.mocked(console.error);

    expect(deleteDocumentKeepalive("7")).toBe(true);
    await flushMicrotasks();

    const logged = errorSpy.mock.calls.map((args) => args.join(" "));
    expect(logged.some((line) => line.includes("did not succeed"))).toBe(true);
    expect(logged.some((line) => line.includes("403"))).toBe(true);
  });

  it("fails closed without sending when no CSRF credential exists", () => {
    // No cookie, no cached token: nothing may leave the page — the caller
    // keeps the id for the bfcache pageshow replay instead.
    expect(deleteDocumentKeepalive("7")).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("logs when the keepalive delete does not reach the server, and never throws", async () => {
    document.cookie = "X-CSRF-Token=tok";
    fetchMock.mockRejectedValue(new Error("document is unloading"));
    const errorSpy = vi.mocked(console.error);

    expect(deleteDocumentKeepalive("7")).toBe(true);
    await flushMicrotasks();

    const logged = errorSpy.mock.calls.map((args) => args.join(" "));
    expect(logged.some((line) => line.includes("did not reach the server"))).toBe(true);
  });
});
