// frontend/src/hooks/useJobStatus.test.ts
// Issue-trace 783-kms-jobhandle-ingest-cancel — behavior pins for the shared
// job-status hook (companion to the frozen C6 contract test in
// useJobStatus.m03.test.ts): terminal statuses end polling exactly once,
// unmount aborts the loop with no late callbacks, the iteration cap fires
// onTimeout, and a family's per-family defaults apply when the caller does
// not override them.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

import {
  JOB_FAMILY_POLL_DEFAULTS,
  useJobStatus,
  type JobStatus,
} from "@/hooks/useJobStatus";

async function advance(ms: number) {
  for (let i = 0; i < Math.ceil(ms / 1000); i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
  }
}

describe("useJobStatus (issue-trace 783)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("ends polling exactly once on a terminal status", async () => {
    const fetchJob = vi
      .fn()
      .mockResolvedValue({ status: "pending" } as JobStatus)
      .mockResolvedValueOnce({ status: "running" } as JobStatus)
      .mockResolvedValueOnce({ status: "running" } as JobStatus)
      .mockResolvedValueOnce({ status: "completed" } as JobStatus);
    const onTerminal = vi.fn();

    const { result } = renderHook(() =>
      useJobStatus("kms", { fetchJob, onTerminal })
    );

    expect(result.current.active).toBe(false);
    act(() => result.current.start());
    expect(result.current.active).toBe(true);

    await advance(JOB_FAMILY_POLL_DEFAULTS.kms.intervalMs * 4);

    expect(onTerminal).toHaveBeenCalledTimes(1);
    expect(onTerminal).toHaveBeenCalledWith({ status: "completed" });
    expect(result.current.active).toBe(false);
    // The loop stopped: no further fetches after the terminal observation.
    const callsAtTerminal = fetchJob.mock.calls.length;
    await advance(JOB_FAMILY_POLL_DEFAULTS.kms.intervalMs * 3);
    expect(fetchJob.mock.calls.length).toBe(callsAtTerminal);
  });

  it("aborts on unmount: no late callbacks, no further fetches", async () => {
    const fetchJob = vi.fn().mockResolvedValue({ status: "running" } as JobStatus);
    const onTerminal = vi.fn();

    const { result, unmount } = renderHook(() =>
      useJobStatus("kms", { fetchJob, onTerminal })
    );
    act(() => result.current.start());
    await advance(JOB_FAMILY_POLL_DEFAULTS.kms.intervalMs);
    unmount();

    const callsAtUnmount = fetchJob.mock.calls.length;
    await advance(JOB_FAMILY_POLL_DEFAULTS.kms.intervalMs * 5);
    expect(fetchJob.mock.calls.length).toBe(callsAtUnmount);
    expect(onTerminal).not.toHaveBeenCalled();
  });

  it("fires onTimeout at the iteration cap when never terminal", async () => {
    const fetchJob = vi.fn().mockResolvedValue({ status: "running" } as JobStatus);
    const onTerminal = vi.fn();
    const onTimeout = vi.fn();
    const cap = 3;

    const { result } = renderHook(() =>
      useJobStatus("kms", { fetchJob, onTerminal, onTimeout, maxIterations: cap })
    );
    act(() => result.current.start());
    await advance(JOB_FAMILY_POLL_DEFAULTS.kms.intervalMs * (cap + 2));

    expect(onTimeout).toHaveBeenCalledTimes(1);
    expect(onTerminal).not.toHaveBeenCalled();
    expect(result.current.active).toBe(false);
  });

  it("keeps polling past a transiently absent or throwing fetch", async () => {
    const fetchJob = vi
      .fn()
      .mockRejectedValueOnce(new Error("transient"))
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce({ status: "failed", error: "boom" } as JobStatus);
    const onTerminal = vi.fn();

    const hook = renderHook(() =>
      useJobStatus("kms", { fetchJob, onTerminal })
    );
    act(() => hook.result.current.start());
    await advance(JOB_FAMILY_POLL_DEFAULTS.kms.intervalMs * 4);

    expect(onTerminal).toHaveBeenCalledTimes(1);
    expect(onTerminal).toHaveBeenCalledWith({ status: "failed", error: "boom" });
  });
});
