import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useEscapeToStop } from "./useEscapeToStop";

function pressEscape(defaultPrevented = false) {
  const event = new KeyboardEvent("keydown", {
    key: "Escape",
    cancelable: true,
  });
  if (defaultPrevented) event.preventDefault();
  window.dispatchEvent(event);
  return event;
}

describe("useEscapeToStop", () => {
  it("calls onStop when active and Escape is pressed", () => {
    const onStop = vi.fn();
    renderHook(() => useEscapeToStop(true, onStop));

    pressEscape();
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("does nothing when inactive", () => {
    const onStop = vi.fn();
    renderHook(() => useEscapeToStop(false, onStop));

    pressEscape();
    expect(onStop).not.toHaveBeenCalled();
  });

  it("ignores Escape already handled elsewhere (defaultPrevented)", () => {
    const onStop = vi.fn();
    renderHook(() => useEscapeToStop(true, onStop));

    pressEscape(true);
    expect(onStop).not.toHaveBeenCalled();
  });

  it("ignores non-Escape keys", () => {
    const onStop = vi.fn();
    renderHook(() => useEscapeToStop(true, onStop));

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    expect(onStop).not.toHaveBeenCalled();
  });

  it("removes the listener on unmount", () => {
    const onStop = vi.fn();
    const { unmount } = renderHook(() => useEscapeToStop(true, onStop));

    unmount();
    pressEscape();
    expect(onStop).not.toHaveBeenCalled();
  });
});
