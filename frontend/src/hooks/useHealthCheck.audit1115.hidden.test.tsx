/**
 * Arm's-length acceptance check for the activity-API trace (audit
 * 1115-1157-1158): useHealthCheck must issue NO GET /health request while
 * document.hidden is true — neither the mount check nor any poll tick — and
 * resume when the document becomes visible again. At base the mount check
 * fires unconditionally (useHealthCheck.ts runs `void checkHealth()` in the
 * effect with no visibility gate) and the setInterval tick polls regardless,
 * so the hidden leg is RED at base. The visible-baseline and label-stability
 * legs pin behavior that must keep working (GREEN at base).
 *
 * Mock shape copied from useHealthCheck.issue1040.networkpin.test.tsx
 * (hoisted mockGet; "@/lib/api" mocked with the importActual spread so the
 * real useAuthStore keeps its named exports; default.get = mockGet). The
 * hook reads response.data, so the mock resolves `{ data: HealthResponse }`.
 */
import { cleanup, renderHook } from "@testing-library/react";
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

/** A fully-known-services healthy response. */
function okResponse() {
  return {
    data: {
      status: "ok",
      services: {
        backend: true,
        embeddings: true,
        chat: true,
        vector_store: true,
      },
    },
  };
}

/** Flip jsdom's document.hidden and notify any visibilitychange listener. */
function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    value: hidden,
    configurable: true,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("useHealthCheck hidden-document gating (audit 1115)", () => {
  let priorAuthState: ReturnType<typeof useAuthStore.getState>;

  beforeEach(() => {
    vi.useFakeTimers();
    priorAuthState = useAuthStore.getState();
    useAuthStore.setState({ isAuthenticated: true });
    mockGet.mockReset();
    mockGet.mockImplementation(() => Promise.resolve(okResponse()));
    setHidden(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    useAuthStore.setState(priorAuthState);
    mockGet.mockReset();
    vi.clearAllMocks();
    cleanup();
  });

  it("issues no request while hidden (mount check and poll ticks) and resumes when visible", async () => {
    setHidden(true);

    renderHook(() => useHealthCheck({ pollInterval: 30_000 }));

    // Three full poll intervals elapse entirely hidden: not one request —
    // neither the mount check nor an interval tick may fire.
    await vi.advanceTimersByTimeAsync(90_000);
    expect(mockGet).not.toHaveBeenCalled();

    // Becoming visible resumes polling: the next interval tick reaches the
    // server.
    setHidden(false);
    await vi.advanceTimersByTimeAsync(30_000);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(mockGet.mock.calls.length).toBeGreaterThanOrEqual(1);
  });

  // K-12 (closeout): a caller WITHOUT a poll interval (SettingsPage) has no
  // tick to catch up on after a hidden-tab mount skips the mount check —
  // the visibilitychange re-arm must fire the first check the moment the
  // document becomes visible.
  it("hidden mount without a poll interval re-arms on visibility", async () => {
    setHidden(true);

    renderHook(() => useHealthCheck());

    // Hidden mount: the mount check is gated, and with no interval nothing
    // else can fire.
    await vi.advanceTimersByTimeAsync(60_000);
    expect(mockGet).not.toHaveBeenCalled();

    // Becoming visible fires the re-arm check IMMEDIATELY (no interval
    // needed).
    setHidden(false);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    expect(mockGet.mock.calls.length).toBe(1);
  });

  it("visible baseline: mount check plus interval polling still work (pin)", async () => {
    const { result } = renderHook(() => useHealthCheck({ pollInterval: 30_000 }));

    // The mount check fires immediately while visible...
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));

    // ...and the first interval tick adds a second request.
    await vi.advanceTimersByTimeAsync(30_000);
    await vi.waitFor(() => expect(mockGet.mock.calls.length).toBeGreaterThanOrEqual(2));

    const health = result.current;
    expect(health.backend).toBe(true);
    expect(health.loading).toBe(false);
  });

  it("label stability: an unknown-services recheck never flickers the last-known label away", async () => {
    const { result } = renderHook(() => useHealthCheck({ pollInterval: 30_000 }));

    // Settle on a fully-known healthy reading first.
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(result.current.backend).toBe(true));
    expect(result.current.loading).toBe(false);

    // One unknown-services response (embeddings/chat null) followed by ok
    // again: the recheck window must retain the last-known label rather than
    // flipping the user-visible health to down/unknown.
    mockGet
      .mockResolvedValueOnce({
        data: {
          status: "ok",
          services: { backend: true, embeddings: null, chat: null },
        },
      })
      .mockImplementation(() => Promise.resolve(okResponse()));

    await vi.advanceTimersByTimeAsync(30_000);
    // During the recheck window the settled backend reading is retained.
    expect(result.current.backend).toBe(true);

    // The 2s recheck timer re-probes and settles back to a real reading.
    await vi.advanceTimersByTimeAsync(2_000);
    await vi.waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.backend).toBe(true);
    expect(result.current.embeddings).toBe(true);
    expect(result.current.chat).toBe(true);
  });
});
