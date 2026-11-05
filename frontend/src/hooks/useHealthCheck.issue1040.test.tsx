/**
 * Issue #1040 acceptance check — C1 (AC1, DISCRIMINATING). This file is FROZEN
 * for trace 1040-991-health-401-chat-cap-sync. (Its PRESERVING sibling C2
 * lives in useHealthCheck.issue1040.networkpin.test.tsx so each file's exit
 * code reflects its own check class.)
 *
 * C1: while the auth store holds an authenticated session, consecutive
 * deep-probe rejections carrying the NORMALIZED error shape (plain Error with
 * top-level `status === 401`, the shape `frontend/src/lib/api/core.ts`
 * :644-651 produces for the #774 refresh-transport residual) must be treated
 * as AUTH events, not outages:
 *   - no all-down publish across the streak (never reaches the down gate);
 *   - a silent refresh IS attempted through the canonical store action
 *     (`useAuthStore.getState().refreshToken()`, useAuthStore.ts:714);
 *   - the repo's UNKNOWN-not-DOWN convention holds: loading stays TRUE
 *     (checking/unknown) and the prior healthy booleans are retained;
 *   - the 401s do not pre-charge the failure streak — down still requires
 *     FAILURE_THRESHOLD (2) fresh network-style failures afterwards;
 *   - the auth_unknown publication STAMPS lastChecked (strictly newer than a
 *     timestamp captured before the 401 settled), and a STALE outcome cannot
 *     regress the state: a deferred 401 rejection that settles AFTER a newer
 *     read has published success must not clobber that newer publication.
 * Five legs/tests below (all C1, all DISCRIMINATING: the two no-down streak
 * legs, the silent-refresh/UNKNOWN-retention leg, the no-streak-precharge
 * leg, and the lastChecked/stale-outcome leg) — 5/5 RED at base, GREEN
 * after the fix.
 * Expected: RED at base, GREEN after the fix.
 *
 * The api module is mocked exactly like the existing useHealthCheck suites
 * (spread-importActual so the real `useAuthStore` still sees the named
 * exports it wires at module load); the auth store is the REAL zustand store,
 * state set with `useAuthStore.setState` and restored after each test. The
 * store's `refreshToken` action is spied on the store object (the hook calls
 * `getState()` at event time, so the spy must live on the state the merge
 * carries forward) with `mockResolvedValue(null)` — the refresh is asserted
 * ATTEMPTED, its transport never runs.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockGet = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    default: { get: mockGet },
  };
});

import { useHealthCheck } from "@/hooks/useHealthCheck";
import { useAuthStore } from "@/stores/useAuthStore";
import type { HealthStatus } from "@/types/health";

/** The normalized 401 the interceptor's shared tail rejects with (core.ts
 * :644-651): a plain Error with a TOP-LEVEL `status` property. */
function normalized401(): Error & { status: number } {
  return Object.assign(new Error("Unauthorized"), { status: 401 });
}

/** One healthy deep-probe response (every service up) used to establish a
 * prior success before the 401 streak, so the UNKNOWN-not-DOWN retention
 * assertions have real booleans to retain. */
function healthyResponse(): { data: { status: string; services: Record<string, boolean> } } {
  return {
    data: {
      status: "ok",
      services: { backend: true, embeddings: true, chat: true },
    },
  };
}

/** True only for the all-services-down publish shape the reducer emits on a
 * services failure: every indicator false and loading cleared. A loading /
 * unknown / retained state is NOT down. */
function isAllDown(health: HealthStatus): boolean {
  return (
    health.backend === false &&
    health.embeddings === false &&
    health.chat === false &&
    health.loading === false
  );
}

/** Spy on the REAL auth store's `refreshToken` action (zustand merges carry
 * the spied reference forward through every setState, and the hook resolves
 * it via getState() at event time). */
function spyRefreshToken() {
  return vi.spyOn(useAuthStore.getState(), "refreshToken").mockResolvedValue(null);
}

/** Advance one poll interval and settle it inside act(), so the read's
 * outcome is dispatched AND rendered into the hook result before the caller
 * asserts (the deterministic idiom of useHealthCheck.issue863.ordering). */
async function pollAndSettle(intervalMs: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(intervalMs);
    await vi.advanceTimersByTimeAsync(0);
  });
}

describe("useHealthCheck issue #1040 — a session-expiry 401 is an auth event, not an outage", () => {
  let priorAuthState: ReturnType<typeof useAuthStore.getState>;
  let refreshSpy: ReturnType<typeof spyRefreshToken>;

  beforeEach(() => {
    vi.useFakeTimers();
    priorAuthState = useAuthStore.getState();
    refreshSpy = spyRefreshToken();
  });

  afterEach(() => {
    vi.useRealTimers();
    // Merges the pre-spy state back over the store, which also restores the
    // ORIGINAL refreshToken reference the spy replaced.
    useAuthStore.setState(priorAuthState);
    mockGet.mockReset();
    vi.clearAllMocks();
    cleanup();
  });

  it("C1: two consecutive normalized-401 deep-probe rejections under an authenticated session do not publish the all-down state (RED at base, GREEN after the fix)", async () => {
    useAuthStore.setState({ isAuthenticated: true });
    mockGet.mockImplementation(() => Promise.reject(normalized401()));

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));

    // Mount check (deep, authenticated) + one interval check: the two
    // consecutive 401s that would otherwise cross FAILURE_THRESHOLD = 2.
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(0);

    expect(
      isAllDown(result.current),
      "a session-expiry 401 (normalized shape, top-level status=401) must not render the app as a full service outage; health stays non-down (loading/unknown)",
    ).toBe(false);
  });

  it("C1: a third consecutive 401 still does not advance the streak toward down (RED at base, GREEN after the fix)", async () => {
    useAuthStore.setState({ isAuthenticated: true });
    mockGet.mockImplementation(() => Promise.reject(normalized401()));

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));

    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(3));
    await vi.advanceTimersByTimeAsync(0);

    expect(
      isAllDown(result.current),
      "persistent 401s must keep routing as auth events — the failure streak must never reach the down publish while the rejection carries status=401",
    ).toBe(false);
  });

  it("C1: a 401 under an authenticated session attempts the silent refresh and holds the UNKNOWN state — loading stays true and the prior healthy booleans are retained (RED at base, GREEN after the fix)", async () => {
    useAuthStore.setState({ isAuthenticated: true });
    // One healthy deep probe first (real service booleans to retain), then
    // the 401 streak.
    mockGet
      .mockResolvedValueOnce(healthyResponse())
      .mockRejectedValueOnce(normalized401())
      .mockRejectedValueOnce(normalized401());

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));

    await pollAndSettle(0);
    expect(mockGet).toHaveBeenCalledTimes(1);
    await pollAndSettle(5_000);
    expect(mockGet).toHaveBeenCalledTimes(2);
    await pollAndSettle(5_000);
    expect(mockGet).toHaveBeenCalledTimes(3);

    // The silent refresh was ATTEMPTED through the canonical store action —
    // the 401 path must route to auth, not just swallow the rejection.
    expect(
      refreshSpy.mock.calls.length,
      "a session-expiry 401 must trigger a silent refresh attempt via useAuthStore.getState().refreshToken()",
    ).toBeGreaterThanOrEqual(1);

    // UNKNOWN, not DOWN (repo convention): loading stays true (checking /
    // unknown) and the healthy booleans from the prior success are retained.
    expect(
      result.current.loading,
      "after 401s health must stay in the checking/unknown state — loading true, never a cleared-down publish",
    ).toBe(true);
    expect(result.current.backend, "prior healthy backend value must be retained").toBe(true);
    expect(result.current.embeddings, "prior healthy embeddings value must be retained").toBe(true);
    expect(result.current.chat, "prior healthy chat value must be retained").toBe(true);
    expect(result.current.lastChecked).toBeInstanceOf(Date);
  });

  it("C1: 401s do not pre-charge the failure streak — down requires two fresh network failures after the 401s (RED at base, GREEN after the fix)", async () => {
    useAuthStore.setState({ isAuthenticated: true });
    // Prior success (so the wasLatest-without-success immediate-down clause
    // cannot fire), two 401s, then two plain network failures.
    mockGet
      .mockResolvedValueOnce(healthyResponse())
      .mockRejectedValueOnce(normalized401())
      .mockRejectedValueOnce(normalized401())
      .mockRejectedValueOnce(new Error("Network Error"))
      .mockRejectedValueOnce(new Error("Network Error"));

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));

    await pollAndSettle(0);
    expect(mockGet).toHaveBeenCalledTimes(1);
    await pollAndSettle(5_000);
    expect(mockGet).toHaveBeenCalledTimes(2);
    await pollAndSettle(5_000);
    expect(mockGet).toHaveBeenCalledTimes(3);

    expect(
      isAllDown(result.current),
      "the 401s themselves must not publish down",
    ).toBe(false);

    // First FRESH network-style failure after the 401s: if the 401s had
    // pre-charged the streak, this one would already cross FAILURE_THRESHOLD
    // and publish down.
    await pollAndSettle(5_000);
    expect(mockGet).toHaveBeenCalledTimes(4);
    expect(
      isAllDown(result.current),
      "a single network failure after the 401s must not publish down — the 401s did not pre-charge the failure streak",
    ).toBe(false);

    // Second consecutive network failure reaches FAILURE_THRESHOLD: down IS
    // published (real outages still surface).
    await pollAndSettle(5_000);
    expect(mockGet).toHaveBeenCalledTimes(5);
    expect(
      isAllDown(result.current),
      "two consecutive network-style failures must still publish the all-down state",
    ).toBe(true);
  });

  it("C1: auth_unknown advances lastChecked and cannot be overwritten by a stale outcome (RED at base, GREEN after the fix)", async () => {
    useAuthStore.setState({ isAuthenticated: true });
    // The deferred-promise idiom on the api mock: the STALE check's rejection
    // handle is captured when the mock is invoked, then released only AFTER a
    // newer read has published — simulating a slow transport settling late.
    let rejectStaleCheck: ((error: Error & { status: number }) => void) | undefined;
    mockGet
      // Check #1: a healthy deep probe so a real success (and its
      // lastChecked stamp) precedes the 401.
      .mockResolvedValueOnce(healthyResponse())
      // Check #2: a FRESH 401 — the latest read when it settles, so its
      // auth_unknown publication is the one that must stamp lastChecked.
      .mockRejectedValueOnce(normalized401())
      // Check #3: the deferred 401 — held open so the NEXT read can publish
      // first; its late rejection is the stale outcome.
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            rejectStaleCheck = reject;
          }),
      )
      // Check #4: the newer read's SUCCESS payload.
      .mockResolvedValueOnce(healthyResponse());

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));

    // (a) The auth_unknown publication stamps lastChecked: the 401 that
    // settles while still the latest read must advance lastChecked past a
    // timestamp captured BEFORE the rejection was even in flight.
    await pollAndSettle(0);
    expect(mockGet).toHaveBeenCalledTimes(1);
    const beforeFirst401 = Date.now();
    await pollAndSettle(5_000);
    expect(mockGet).toHaveBeenCalledTimes(2);

    expect(result.current.lastChecked).toBeInstanceOf(Date);
    expect(
      (result.current.lastChecked as Date).getTime(),
      "the auth_unknown publication must stamp lastChecked — strictly newer than a timestamp captured before the rejection; the base instead retains the prior success's stamp on the non-down failure path",
    ).toBeGreaterThan(beforeFirst401);

    // (b) A stale outcome cannot regress the state. Check #3's 401 is held
    // deferred while check #4 completes FIRST with a success payload; only
    // then is the deferred rejection released. The late auth_unknown from the
    // superseded read must not clobber check #4's success.
    await pollAndSettle(5_000);
    expect(mockGet).toHaveBeenCalledTimes(3);
    expect(rejectStaleCheck).toBeDefined();
    await pollAndSettle(5_000);
    expect(mockGet).toHaveBeenCalledTimes(4);

    // The newer success published (the baseline the stale outcome must not
    // regress): snapshot the whole health after check #4 settled.
    const afterNewerSuccess = result.current;
    expect(afterNewerSuccess.backend, "sanity: check #4's success must have published before the stale release").toBe(true);

    // Release the deferred 401 rejection of the superseded read #3 and let
    // its dispatch run.
    await act(async () => {
      rejectStaleCheck?.(normalized401());
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(0);
    });

    // The health still reflects check #4's success — the late auth_unknown
    // from the stale read did not clobber it (no regressed booleans, no
    // rolled-back lastChecked).
    expect(result.current).toEqual(afterNewerSuccess);
    expect(
      result.current.backend,
      "a stale 401 settling after a newer success must not regress the newer publication's booleans",
    ).toBe(true);
    expect(
      (result.current.lastChecked as Date).getTime(),
      "a stale 401 settling after a newer success must not roll lastChecked back below the newer publication's stamp",
    ).toBe((afterNewerSuccess.lastChecked as Date).getTime());
  });
});
