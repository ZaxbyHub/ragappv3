// frontend/src/components/shared/KeyboardShortcuts.capture.test.tsx
// Issue #573 AC4 — PR-review PRR-001 + PRR-003: capture-mode lifecycle and
// conflict handling. Complements the frozen C4 spec (rebind round-trip) and
// SessionRail.rebind.test.tsx (second consumer) without touching either.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, act, renderHook } from "@testing-library/react";

import { useKeyboardShortcuts, KeyboardShortcutsDialog } from "./KeyboardShortcuts";
import { SHORTCUT_BINDINGS_STORAGE_KEY } from "@/lib/shortcutBindings";

const noop = () => {};

describe("KeyboardShortcuts capture lifecycle + conflicts (PRR-001/PRR-003)", () => {
  const storage = new Map<string, string>();

  beforeEach(() => {
    vi.clearAllMocks();
    storage.clear();
    vi.mocked(localStorage.getItem).mockImplementation((key: string) => storage.get(key) ?? null);
    vi.mocked(localStorage.setItem).mockImplementation((key: string, value: string) => {
      storage.set(key, value);
    });
    vi.mocked(localStorage.removeItem).mockImplementation((key: string) => {
      storage.delete(key);
    });
    vi.mocked(localStorage.clear).mockImplementation(() => {
      storage.clear();
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("PRR-001: closing the dialog mid-capture disarms the capture listener", async () => {
    const { rerender } = render(
      <KeyboardShortcutsDialog open={true} onOpenChange={noop} />
    );
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: show keyboard shortcuts/i })
    );
    expect(screen.getByTestId("capture-hint")).toBeInTheDocument();

    // Outside-click close: open flips false while capturing.
    rerender(<KeyboardShortcutsDialog open={false} onOpenChange={noop} />);
    await Promise.resolve();

    // The next app-wide keydown must NOT be swallowed into a binding: it
    // would previously be preventDefault'ed and persisted as the rebind.
    const notPrevented = new KeyboardEvent("keydown", {
      key: "a",
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      window.dispatchEvent(notPrevented);
    });
    expect(notPrevented.defaultPrevented).toBe(false);
    const persisted = storage.get(SHORTCUT_BINDINGS_STORAGE_KEY);
    expect(persisted === undefined || !persisted.includes('"a"')).toBe(true);

    // Reopening the dialog shows no capture hint (capturing was reset).
    rerender(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
    expect(screen.queryByTestId("capture-hint")).not.toBeInTheDocument();
  });

  it("PRR-001: reopen after mid-capture close starts clean (no stale capture)", () => {
    const { rerender } = render(
      <KeyboardShortcutsDialog open={true} onOpenChange={noop} />
    );
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: show keyboard shortcuts/i })
    );
    rerender(<KeyboardShortcutsDialog open={false} onOpenChange={noop} />);
    rerender(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);

    // No capture hint on the freshly reopened dialog…
    expect(screen.queryByTestId("capture-hint")).not.toBeInTheDocument();
    // …and a keydown is processed by the NORMAL hook path, not a capture.
    const { result } = renderHookSafe();
    act(() => {
      fireEvent.keyDown(window, { key: "?", shiftKey: true });
    });
    expect(result.current.open).toBe(true);
  });

  it("PRR-003: rebinding focusSearch onto the showShortcuts combo clears the conflicting binding", () => {
    // Seed: showShortcuts is rebound to F9.
    storage.set(
      SHORTCUT_BINDINGS_STORAGE_KEY,
      JSON.stringify({ showShortcuts: "F9" })
    );
    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);

    // Rebind focusSearch to F9 — the showShortcuts binding must be released.
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: focus session search/i })
    );
    act(() => {
      fireEvent.keyDown(window, { key: "F9" });
    });

    const persisted = JSON.parse(storage.get(SHORTCUT_BINDINGS_STORAGE_KEY) ?? "{}");
    expect(persisted.focusSearch).toBe("F9");
    expect(
      persisted.showShortcuts,
      "the conflicting showShortcuts binding must revert to its default"
    ).toBeUndefined();

    // And behaviorally: exactly one action owns F9 now — showShortcuts
    // reverted to its default "?" (F9 no longer opens the dialog), and the
    // default combo works again.
    const { result } = renderHookSafe();
    act(() => {
      fireEvent.keyDown(window, { key: "F9" });
    });
    expect(result.current.open).toBe(false);
    act(() => {
      fireEvent.keyDown(window, { key: "?", shiftKey: true });
    });
    expect(result.current.open).toBe(true);
  });
});
function renderHookSafe() {
  return renderHook(() => useKeyboardShortcuts());
}
