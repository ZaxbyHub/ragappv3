// Regression tests for the permanent "Chat service unavailable" banner / red
// status badges: lightweight (non-deep) /health polls return
// services.embeddings/chat = null ("not probed"), and the hook previously
// collapsed null to false via `?? false`, marking both services down forever
// from the second poll (~30s after load) even while chat worked.
import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const getMock = vi.fn();

vi.mock("@/lib/api", () => ({
  default: { get: (...args: unknown[]) => getMock(...args) },
}));

import { useHealthCheck } from "./useHealthCheck";

const deepResponse = {
  data: {
    status: "ok",
    services: { backend: true, embeddings: true, chat: true, vector_store: true },
  },
};

// Lightweight poll: backend liveness only; model services not probed.
const lightResponse = {
  data: {
    status: "ok",
    services: { backend: true, embeddings: null, chat: null, vector_store: null },
  },
};

describe("useHealthCheck", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    getMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("sends deep=true on the first poll", async () => {
    getMock.mockResolvedValue(deepResponse);

    renderHook(() => useHealthCheck());
    await act(async () => {
      await vi.runOnlyPendingTimersAsync();
    });

    expect(getMock).toHaveBeenCalledWith("/health", { params: { deep: true } });
  });

  it("keeps chat/embeddings up across lightweight polls returning null services", async () => {
    getMock
      .mockResolvedValueOnce(deepResponse)
      .mockResolvedValue(lightResponse);

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 1000 }));

    await act(async () => {
      await vi.runOnlyPendingTimersAsync();
    });
    expect(result.current.chat).toBe(true);
    expect(result.current.embeddings).toBe(true);

    // Several lightweight polls follow; null must mean "keep last known",
    // not "down".
    for (let i = 0; i < 3; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
    }

    expect(result.current.backend).toBe(true);
    expect(result.current.chat).toBe(true);
    expect(result.current.embeddings).toBe(true);
  });

  it("re-probes deep on the 10th poll and picks up a real outage", async () => {
    const deepDown = {
      data: {
        status: "ok",
        services: { backend: true, embeddings: false, chat: false, vector_store: true },
      },
    };
    getMock.mockImplementation((_url: string, opts: { params?: { deep?: boolean } }) =>
      Promise.resolve(opts?.params?.deep ? deepDown : lightResponse)
    );
    // First deep poll reports down; make it healthy for that one only.
    getMock.mockResolvedValueOnce(deepResponse);

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 1000 }));
    await act(async () => {
      await vi.runOnlyPendingTimersAsync();
    });
    expect(result.current.chat).toBe(true);

    // Polls 2-10: lightweight. Poll 11 (index 10) is the next deep probe.
    for (let i = 0; i < 10; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
    }

    const deepCalls = getMock.mock.calls.filter(
      (c) => (c[1] as { params?: { deep?: boolean } })?.params?.deep
    );
    expect(deepCalls.length).toBeGreaterThanOrEqual(2);
    expect(result.current.chat).toBe(false);
    expect(result.current.embeddings).toBe(false);
  });

  it("marks everything down when the poll itself fails", async () => {
    // First poll succeeds (deep), the next rejects — the catch path must
    // mark every service down (backend genuinely unreachable).
    getMock.mockResolvedValueOnce(deepResponse).mockRejectedValue(new Error("net"));

    const { result } = renderHook(() => useHealthCheck({ pollInterval: 1000 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(result.current.backend).toBe(false);
    expect(result.current.chat).toBe(false);
    expect(result.current.embeddings).toBe(false);
  });
});
