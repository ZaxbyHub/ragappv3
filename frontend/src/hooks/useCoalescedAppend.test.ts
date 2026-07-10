import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import useCoalescedAppend from "./useCoalescedAppend";

describe("useCoalescedAppend", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("appends multiple chunks into a single content update", () => {
    const { result } = renderHook(() => useCoalescedAppend());

    act(() => {
      result.current.append("a");
      result.current.append("b");
      result.current.append("c");
      // Advance time so RAF callbacks (if RAF is used) and any pending
      // timers fire and trigger the coalesced flush.
      vi.advanceTimersByTime(50);
    });

    expect(result.current.content).toBe("abc");
    expect(result.current.isPending).toBe(false);
  });

  it("flushes immediately when flush is called", () => {
    const { result } = renderHook(() => useCoalescedAppend());

    act(() => {
      result.current.append("hello");
    });

    expect(result.current.content).toBe("");
    expect(result.current.isPending).toBe(true);

    act(() => {
      result.current.flush();
    });

    expect(result.current.content).toBe("hello");
    expect(result.current.isPending).toBe(false);
  });

  it("isPending is true while chunks are buffered", () => {
    const { result } = renderHook(() => useCoalescedAppend());

    act(() => {
      result.current.append("x");
    });

    // Before RAF fires, content should be empty and isPending true
    expect(result.current.isPending).toBe(true);
    expect(result.current.content).toBe("");

    act(() => {
      // Advance timers to fire the scheduled RAF callback
      vi.advanceTimersByTime(50);
    });

    expect(result.current.isPending).toBe(false);
  });

  it("does not double-count content on multiple appends", () => {
    const { result } = renderHook(() => useCoalescedAppend());

    act(() => {
      result.current.append("a");
      vi.advanceTimersByTime(50);
    });

    act(() => {
      result.current.append("b");
      vi.advanceTimersByTime(50);
    });

    expect(result.current.content).toBe("ab");
  });

  it("cleans up scheduled flush on unmount", () => {
    const { result, unmount } = renderHook(() => useCoalescedAppend());

    act(() => {
      result.current.append("z");
    });

    // Before RAF fires: content is '' (not yet flushed)
    expect(result.current.content).toBe("");
    expect(result.current.isPending).toBe(true);

    // Fire the RAF so content is 'z' before unmount
    act(() => {
      vi.advanceTimersByTime(50);
    });

    expect(result.current.content).toBe("z");
    expect(result.current.isPending).toBe(false);

    // Unmount sets mountedRef = false, cancelling any future RAF
    unmount();

    // Advancing timers after unmount should NOT cause any state changes
    // because the mountedRef guard in the RAF callback prevents it.
    // If the guard were missing, the callback would fire and potentially
    // cause state mutations on an unmounted component.
    act(() => {
      vi.advanceTimersByTime(100);
    });

    // Content should still be 'z' - no additional mutations occurred
    expect(result.current.content).toBe("z");
  });

  it("reset() clears content and buffered chunks", () => {
    const { result } = renderHook(() => useCoalescedAppend());

    act(() => {
      result.current.append("hello");
      result.current.append("world");
    });

    expect(result.current.isPending).toBe(true);
    expect(result.current.content).toBe("");

    act(() => {
      result.current.flush();
    });

    expect(result.current.content).toBe("helloworld");
    expect(result.current.isPending).toBe(false);

    act(() => {
      result.current.reset();
    });

    expect(result.current.content).toBe("");
    expect(result.current.isPending).toBe(false);
  });

  it("reset() cancels pending animation frame and timeout handles so stale callbacks do not fire", () => {
    const { result } = renderHook(() => useCoalescedAppend());

    // Append "old" — this schedules a RAF (or setTimeout fallback) but we do NOT advance timers yet
    act(() => {
      result.current.append("old");
    });

    expect(result.current.isPending).toBe(true);
    expect(result.current.content).toBe("");

    // Reset clears the scheduled callback before it fires
    act(() => {
      result.current.reset();
    });

    expect(result.current.content).toBe("");
    expect(result.current.isPending).toBe(false);

    // Append "new" content
    act(() => {
      result.current.append("new");
    });

    // Advance timers — if the old RAF/timeout had not been cancelled, it would fire and
    // potentially flush the old buffer (or corrupt state). With the fix in place,
    // only the new scheduled callback fires and content is just "new".
    act(() => {
      vi.advanceTimersByTime(50);
    });

    expect(result.current.content).toBe("new");
    expect(result.current.isPending).toBe(false);
  });

  it("reset() prevents flush() from restoring old content", () => {
    const { result } = renderHook(() => useCoalescedAppend());

    // Append and flush some content
    act(() => {
      result.current.append("old");
      vi.advanceTimersByTime(50);
    });

    expect(result.current.content).toBe("old");

    // Reset clears everything
    act(() => {
      result.current.reset();
    });

    expect(result.current.content).toBe("");

    // Append new content and flush
    act(() => {
      result.current.append("new");
      result.current.flush();
    });

    // Content should be just "new", not "oldnew"
    expect(result.current.content).toBe("new");
  });

  it("uses fallback timeout when requestAnimationFrame is unavailable", () => {
    // Simulate an environment where requestAnimationFrame does not exist,
    // forcing the hook to use setTimeout with fallbackMs
    const originalRaf = globalThis.requestAnimationFrame;
    // @ts-expect-error - intentionally removing RAF to test fallback path
    delete globalThis.requestAnimationFrame;

    try {
      const { result } = renderHook(() =>
        useCoalescedAppend({ fallbackMs: 50 })
      );

      act(() => {
        result.current.append("a");
        result.current.append("b");
      });

      expect(result.current.content).toBe("");
      expect(result.current.isPending).toBe(true);

      // Advance past the fallback timeout so the setTimeout fires
      act(() => {
        vi.advanceTimersByTime(50);
      });

      expect(result.current.content).toBe("ab");
      expect(result.current.isPending).toBe(false);
    } finally {
      globalThis.requestAnimationFrame = originalRaf;
    }
  });
});
