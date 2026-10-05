/**
 * Issue #774 L03 red checkpoint (AC8): the /csrf-token fetch has no
 * signal/timeout, and the singleton _csrfFetchPromise (cleared only in its
 * .finally) gates every mutating request — a hung fetch wedges CSRF
 * acquisition forever, so a retry must issue a NEW request once the hung one
 * has had ample time to time out.
 *
 * The stub fetch never settles on its own (a hung server) and only rejects
 * when the request init's abort signal fires, so the test stays independent
 * of which primitive the fix uses — only that 120s of fake time is far beyond
 * any sane timeout constant.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ensureCsrfToken, resetCsrfToken } from "./core";

describe("issue 774 csrf token fetch timeout", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    // Fresh singleton state and a clean cookie jar (fresh jsdom has none, so
    // the cookie fast-path cannot skip the network fetch).
    resetCsrfToken();
    vi.useFakeTimers();
    fetchMock = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("a hung csrf-token fetch times out and a retry issues a new request", async () => {
    // First acquisition hangs (the underlying fetch never settles unless
    // aborted). Attach a catch so a later abort-driven rejection is handled.
    void ensureCsrfToken().catch(() => undefined);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // 120s of fake time — far beyond any sane timeout constant.
    await vi.advanceTimersByTimeAsync(120_000);

    // After that window a retry must issue a NEW fetch instead of reusing
    // the wedged singleton promise.
    const second = ensureCsrfToken();
    second.catch(() => undefined);
    await vi.advanceTimersByTimeAsync(1);

    expect(fetchMock.mock.calls.length).toBe(2);
  });
});
