/**
 * Issue #494 acceptance check — AC6 / OPS-008 (PRESERVING).
 *
 * Pins `useHealthCheck` (frontend/src/hooks/useHealthCheck.ts, landed via
 * PR #493): a healthy deep check followed by lightweight (shallow) polls that
 * return HTTP 200 with `null` service values ("not checked this cycle") must
 * RETAIN the last-known indicators — null must never be coerced to `false`.
 *
 * Gap analysis (why this file exists): the existing suite
 * `frontend/src/tests/useHealthCheck.test.tsx` covers the fetch-REJECTION
 * contract (one failure after a success retains; two consecutive failures
 * flip down; cold-start failure surfaces immediately) but never feeds a 200
 * response with `null`/absent service values, so the
 * `services?.embeddings ?? prev.embeddings` retention path was unasserted.
 * The two-consecutive-failures-DO half of the AC remains covered by the
 * existing file's first node ("retains last-known on one failure after a
 * success; flips down on two consecutive"); this node covers exactly the
 * healthy-then-null half, plus the null-vs-false contrast.
 *
 * Wire-level facts asserted alongside the state: the mount check is sent with
 * `{ params: { deep: true } }` and the interval polls with `{ params: {} }`
 * (lightweight), read from the mocked api client's call log.
 */
import { renderHook } from "@testing-library/react";
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

const healthyDeepResponse = {
  data: {
    status: "ok",
    services: { backend: true, embeddings: true, chat: true },
  },
};

/** Lightweight poll body: backend reachable, services "not checked". */
const nullServicesResponse = {
  data: {
    status: "ok",
    services: { backend: true, embeddings: null, chat: null },
  },
};

/** Explicit-false poll body: backend authoritative "down" verdicts. */
const explicitFalseResponse = {
  data: {
    status: "ok",
    services: { backend: true, embeddings: false, chat: false },
  },
};

describe("useHealthCheck — last-known retention on null probes (issue #494 AC6/OPS-008, PRESERVING)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    mockGet.mockReset();
    vi.clearAllMocks();
  });

  it("retains indicators when lightweight polls return null services; explicit false still flips down", async () => {
    mockGet.mockResolvedValue(healthyDeepResponse as never);
    const { result } = renderHook(() => useHealthCheck({ pollInterval: 5_000 }));

    // Mount check is deep (first check): sent as /health with params deep=true.
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(result.current.backend).toBe(true));
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet.mock.calls[0][0]).toBe("/health");
    expect(mockGet.mock.calls[0][1]).toEqual({ params: { deep: true } });
    expect(result.current.embeddings).toBe(true);
    expect(result.current.chat).toBe(true);
    expect(result.current.loading).toBe(false);

    // Lightweight poll returns 200 with null services: indicators must NOT
    // flip down; the request itself must be lightweight (params: {}).
    mockGet.mockResolvedValueOnce(nullServicesResponse as never);
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(mockGet.mock.calls[1][1]).toEqual({ params: {} });
    expect(result.current.backend).toBe(true);
    expect(result.current.embeddings).toBe(true); // retained, not coerced to false
    expect(result.current.chat).toBe(true); // retained, not coerced to false
    expect(result.current.loading).toBe(false);

    // A second consecutive null lightweight poll STILL retains: null is
    // "not checked", not a failure — no 2-failure threshold applies to it.
    mockGet.mockResolvedValueOnce(nullServicesResponse as never);
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(3));
    expect(result.current.embeddings).toBe(true);
    expect(result.current.chat).toBe(true);
    expect(result.current.backend).toBe(true);

    // Contrast: an explicit false from the backend is authoritative and flips
    // the indicators down immediately (pins null != false).
    mockGet.mockResolvedValueOnce(explicitFalseResponse as never);
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(result.current.embeddings).toBe(false));
    expect(result.current.chat).toBe(false);
    expect(result.current.backend).toBe(true);

    console.log(
      "PRESERVING GREEN: AC6/OPS-008 useHealthCheck last-known retention — " +
        "healthy deep check (params deep=true) then lightweight polls " +
        "(params {}) returning null services retain embeddings/chat " +
        "indicators across consecutive polls; explicit false flips them down",
    );
  });
});
