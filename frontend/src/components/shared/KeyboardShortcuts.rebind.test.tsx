// frontend/src/components/shared/KeyboardShortcuts.rebind.test.tsx
// Issue #573 (AC4) — acceptance check C4: keyboard-shortcut rebinding.
//
// DISCRIMINATING check: expected RED at base commit ae2e15a0 —
// KeyboardShortcuts.tsx ships a static shortcuts array, a hardcoded "?"
// listener, and a static dialog; no rebind UI, no persistence. GREEN once the
// implementer extends the existing module per the contract below.
//
// Frozen contract (extension of the EXISTING
// frontend/src/components/shared/KeyboardShortcuts.tsx — no new module):
//   KeyboardShortcutsDialog:
//     - the "Show keyboard shortcuts" row exposes a rebind control whose
//       accessible name matches /rebind shortcut: show keyboard shortcuts/i;
//     - activating it enters capture mode: the dialog shows a
//       waiting-for-keys hint matching /press/i;
//     - while capturing, a WINDOW-level keydown records the pressed combo
//       (e.g. "F9") and exits capture mode;
//     - a reset control (accessible name matching /reset/i) restores the
//       default bindings and clears the persisted customization.
//   Persistence (zustand persist pattern, cf. useThemeStore "kv-theme"):
//     a localStorage key STARTING WITH "kv-keyboard-shortcuts" whose value
//     contains the bound combo string after a rebind, and no custom combo
//     after a reset.
//   useKeyboardShortcuts hook:
//     a FRESH mount honors the persisted binding — the bound key opens the
//     dialog and the default "?" no longer does; after reset, "?" opens
//     again and the custom key does not.
//
// localStorage: per-suite in-memory mock (Composer.draft.test.tsx pattern)
// layered over the global vi.fn stubs from src/test/setup.ts.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, cleanup, renderHook } from "@testing-library/react";

import { useKeyboardShortcuts, KeyboardShortcutsDialog } from "./KeyboardShortcuts";

const noop = () => {};

const isOpen = (result: { current: { open: boolean } }) => result.current.open;

describe("Keyboard shortcut rebinding (issue #573 AC4 / C4)", () => {
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

  it("sanity: the default '?' binding still opens the dialog with no customization", () => {
    const { result } = renderHook(() => useKeyboardShortcuts());
    expect(isOpen(result)).toBe(false);

    act(() => {
      fireEvent.keyDown(window, { key: "?", shiftKey: true, ctrlKey: false, metaKey: false });
    });
    expect(isOpen(result)).toBe(true);
  });

  it("rebind flow: capture a new combo, persist it under a kv-keyboard-shortcuts key, and honor it in a fresh hook", () => {
    // 1. Open the dialog and enter rebind mode for the "Show keyboard
    //    shortcuts" row.
    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);

    const rebindButton = screen.getByRole("button", {
      name: /rebind shortcut: show keyboard shortcuts/i,
    });
    fireEvent.click(rebindButton);

    expect(
      screen.getByText(/press/i),
      "entering rebind mode must show a waiting-for-keys hint (issue #573 AC4)"
    ).toBeInTheDocument();

    // 2. Capture the new combo via a window-level keydown.
    act(() => {
      fireEvent.keyDown(window, { key: "F9" });
    });

    const persistedKey = Array.from(storage.keys()).find((k) =>
      k.startsWith("kv-keyboard-shortcuts")
    );
    expect(
      persistedKey,
      "a rebind must persist under a localStorage key starting with 'kv-keyboard-shortcuts' (zustand persist pattern, cf. kv-theme)"
    ).toBeDefined();
    expect(storage.get(persistedKey!)).toContain("F9");

    // 3. A FRESH hook mount honors the persisted binding: F9 opens...
    const bound = renderHook(() => useKeyboardShortcuts());
    act(() => {
      fireEvent.keyDown(window, { key: "F9" });
    });
    expect(
      isOpen(bound.result),
      "the persisted combo must open the shortcuts dialog (issue #573 AC4)"
    ).toBe(true);
    bound.unmount();

    // ...and the default "?" no longer does.
    const afterRebind = renderHook(() => useKeyboardShortcuts());
    act(() => {
      fireEvent.keyDown(window, { key: "?", shiftKey: true, ctrlKey: false, metaKey: false });
    });
    expect(
      isOpen(afterRebind.result),
      "the default '?' must no longer open the dialog while a custom binding is persisted (issue #573 AC4)"
    ).toBe(false);
  });

  it("reset restores the default '?' binding and drops the custom combo", () => {
    // Seed persisted customization directly (the persisted shape is the
    // implementer's; only the key prefix and the combo substring are contract).
    storage.set("kv-keyboard-shortcuts", JSON.stringify({ showShortcuts: "F9" }));

    const bound = renderHook(() => useKeyboardShortcuts());
    act(() => {
      fireEvent.keyDown(window, { key: "F9" });
    });
    expect(isOpen(bound.result)).toBe(true);
    bound.unmount();

    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
    fireEvent.click(screen.getByRole("button", { name: /reset/i }));

    const persistedKey = Array.from(storage.keys()).find((k) =>
      k.startsWith("kv-keyboard-shortcuts")
    );
    const persistedValue = persistedKey ? storage.get(persistedKey) ?? "" : "";
    expect(
      persistedValue.includes("F9"),
      "reset must clear the persisted custom combo (issue #573 AC4)"
    ).toBe(false);

    const afterReset = renderHook(() => useKeyboardShortcuts());
    act(() => {
      fireEvent.keyDown(window, { key: "?", shiftKey: true, ctrlKey: false, metaKey: false });
    });
    expect(
      isOpen(afterReset.result),
      "after reset the default '?' must open the dialog again (issue #573 AC4)"
    ).toBe(true);
    afterReset.result.current.setOpen(false);
    afterReset.unmount();

    const f9AfterReset = renderHook(() => useKeyboardShortcuts());
    act(() => {
      fireEvent.keyDown(window, { key: "F9" });
    });
    expect(
      isOpen(f9AfterReset.result),
      "after reset the abandoned custom key must no longer open the dialog (issue #573 AC4)"
    ).toBe(false);
  });
});
