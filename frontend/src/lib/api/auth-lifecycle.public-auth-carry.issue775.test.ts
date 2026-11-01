import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getJwtAccessToken,
  refreshAccessToken,
  resetCsrfToken,
  setJwtAccessToken,
} from "@/lib/api/core";
import {
  captureAuthOwner,
  enqueueAuthTransport,
  isCurrentAuthOwner,
  onAuthOwnerReplacement,
  reserveReplacementAuthOwner,
  StaleAuthOwnerError,
} from "@/lib/api/auth-lifecycle";

describe("new auth-lifecycle owner contract for issue #775", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    resetCsrfToken();
    setJwtAccessToken(null);
  });

  it("replaces synchronously, aborts the prior owner, and rejects stale use", async () => {
    const previous = captureAuthOwner();
    const replacement = reserveReplacementAuthOwner();
    const work = vi.fn(async () => undefined);

    expect(previous.signal.aborted).toBe(true);
    expect(isCurrentAuthOwner(previous)).toBe(false);
    expect(isCurrentAuthOwner(replacement)).toBe(true);
    await expect(enqueueAuthTransport(previous, work)).rejects.toBeInstanceOf(StaleAuthOwnerError);
    expect(work).not.toHaveBeenCalled();
  });

  it("stops replacement listener delivery when a listener reenters with a newer owner", () => {
    let reentered = false;
    const listener = vi.fn(() => {
      if (!reentered) {
        reentered = true;
        reserveReplacementAuthOwner();
      }
    });
    const unsubscribe = onAuthOwnerReplacement(listener);

    try {
      const returned = reserveReplacementAuthOwner();

      expect(isCurrentAuthOwner(returned)).toBe(false);
      expect(listener).toHaveBeenCalledTimes(2);
      expect(isCurrentAuthOwner(captureAuthOwner())).toBe(true);
    } finally {
      unsubscribe();
    }
  });

  it("admits a fresh owner to exactly one credential transport", async () => {
    const owner = reserveReplacementAuthOwner();
    const work = vi.fn(async () => "accepted");

    await expect(enqueueAuthTransport(owner, work)).resolves.toBe("accepted");
    expect(work).toHaveBeenCalledTimes(1);
  });

  it("rejects a replaced owner before work and admits its successor", async () => {
    const previous = captureAuthOwner();
    const successor = reserveReplacementAuthOwner();
    const staleWork = vi.fn(async () => "stale");
    const successorWork = vi.fn(async () => "accepted");

    await expect(enqueueAuthTransport(previous, staleWork)).rejects.toBeInstanceOf(StaleAuthOwnerError);
    expect(staleWork).not.toHaveBeenCalled();
    await expect(enqueueAuthTransport(successor, successorWork)).resolves.toBe("accepted");
    expect(successorWork).toHaveBeenCalledTimes(1);
  });

  it("returns a logical deadline for a replaced owner without publishing its delayed body", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    resetCsrfToken();
    setJwtAccessToken("old-token");

    let resolveBody!: (value: unknown) => void;
    let bodySettled = false;
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: () => Promise.resolve({ csrf_token: "csrf-owner-deadline" }),
    });
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => {
        const body = await new Promise<unknown>((resolve) => {
          resolveBody = resolve;
        });
        bodySettled = true;
        return body;
      },
    });

    const ownerA = captureAuthOwner();
    const outcome = refreshAccessToken(ownerA).then(
      (value) => ({ status: "fulfilled" as const, value }),
      (error) => ({ status: "rejected" as const, error }),
    );
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    reserveReplacementAuthOwner();
    await vi.advanceTimersByTimeAsync(10_001);
    await expect(outcome).resolves.toMatchObject({ status: "rejected" });
    expect(getJwtAccessToken()).not.toBe("stale-token");
    expect(bodySettled).toBe(false);

    resolveBody({ access_token: "stale-token" });
    await vi.waitFor(() => expect(bodySettled).toBe(true));
    expect(getJwtAccessToken()).not.toBe("stale-token");
  });
});
