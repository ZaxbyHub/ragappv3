/**
 * Feedback-round pin (OOB review F-005) for the #774 stall guard — the frozen
 * l03 spec only asserts "something settled"; this file pins the boundary and
 * the exact teardown contract:
 *   - at 149_999ms of silence: nothing settled, onError not called;
 *   - at +1ms: onError fires EXACTLY ONCE with ChatInterruptedError,
 *     onComplete is never called, and the reader is cancelled exactly once.
 * (New unfrozen file: the frozen checkpoint manifest is byte-locked.)
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseSSEStream } from "./sessions";

// The documented contract value: 150_000ms = 10 x 15s backend heartbeat.
const STALL_MS = 150_000;

function stalledReader() {
  return {
    read: vi.fn(
      () =>
        new Promise<{ value?: Uint8Array; done: boolean }>(() => undefined)
    ),
    cancel: vi.fn(() => Promise.resolve()),
  };
}

describe("issue 774 stall guard boundary (feedback pin)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not settle before the 150s inactivity window elapses", async () => {
    const reader = stalledReader();
    const onError = vi.fn();
    const onComplete = vi.fn();
    const p = parseSSEStream(
      reader as unknown as ReadableStreamDefaultReader<Uint8Array>,
      { onMessage: () => undefined, onComplete, onError }
    );
    p.then(() => undefined, () => undefined);

    await vi.advanceTimersByTimeAsync(STALL_MS - 1);

    expect(onError).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();
    expect(reader.cancel).not.toHaveBeenCalled();
    // The inactivity timer is still armed — nothing has settled.
    expect(vi.getTimerCount()).toBeGreaterThan(0);
  });

  it("at +1ms fires onError once with ChatInterruptedError, never onComplete, and cancels the reader", async () => {
    const reader = stalledReader();
    const onError = vi.fn();
    const onComplete = vi.fn();
    const p = parseSSEStream(
      reader as unknown as ReadableStreamDefaultReader<Uint8Array>,
      { onMessage: () => undefined, onComplete, onError }
    );

    const settled: string[] = [];
    p.then(
      () => settled.push("resolved"),
      () => settled.push("rejected")
    );

    await vi.advanceTimersByTimeAsync(STALL_MS + 1);

    expect(onError).toHaveBeenCalledTimes(1);
    const err = onError.mock.calls[0][0] as Error;
    expect(err.name).toBe("ChatInterruptedError");
    expect(err.message).not.toMatch(/abort/i);
    expect(onComplete).not.toHaveBeenCalled();
    expect(reader.cancel).toHaveBeenCalledTimes(1);
    // The stall path signals via onError and the promise resolves (matching
    // the CHAT-004 interrupted contract) — it never rejects.
    expect(settled).toEqual(["resolved"]);
  });
});
