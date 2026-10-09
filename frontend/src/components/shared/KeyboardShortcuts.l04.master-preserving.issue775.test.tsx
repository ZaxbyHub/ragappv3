// frontend/src/components/shared/KeyboardShortcuts.l04.test.tsx
// L04 (frozen acceptance checks) — shortcut dispatch ownership.
//
// B1 / AC2: "a persisted showShortcuts combo always fires" — the rebind
// dialog captures and persists modifier combos for showShortcuts
// (comboFromEvent normalizes Ctrl+J, stored under "kv-keyboard-shortcuts"),
// but useKeyboardShortcuts returns early on ANY ctrl/meta-modified event
// (`if (e.ctrlKey || e.metaKey) return;`), so a persisted modifier combo
// can never open the dialog. Expected RED: "expected true to be false".
//
// B2 / AC3: "a printable focusSearch combo never steals focus from a
// textarea" — the rebind dialog accepts an unmodified "j" for focusSearch,
// and ChatSearchInput's document-level listener matches ANY keydown (no
// input/textarea/contentEditable target guard), so typing "j" into the
// composer textarea yanks focus to the session search. Expected RED:
// "expected false to be true".
//
// Harness: the KeyboardShortcuts.rebind.test.tsx rebind flow — per-suite
// in-memory localStorage Map mock layered over the global vi.fn stubs from
// src/test/setup.ts. Static imports + hoisted vi.mock interception only
// (no dynamic imports racing the mock registry).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, cleanup, renderHook } from "@testing-library/react";

import { useKeyboardShortcuts, KeyboardShortcutsDialog } from "./KeyboardShortcuts";
import { ChatSearchInput } from "@/components/chat/SessionRail";

const noop = () => {};

describe("Keyboard shortcut dispatch ownership (L04)", () => {
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

  it("a persisted showShortcuts combo always fires", () => {
    // 1. Capture Ctrl+J for "Show keyboard shortcuts" through the rebind UI
    //    (the KeyboardShortcuts.rebind.test.tsx rebind-flow harness).
    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: show keyboard shortcuts/i })
    );
    act(() => {
      fireEvent.keyDown(window, { key: "j", ctrlKey: true });
    });

    // Did the capture persist "Ctrl+J" for showShortcuts?
    const persistedKey = Array.from(storage.keys()).find((k) =>
      k.startsWith("kv-keyboard-shortcuts")
    );
    const persistedContainsCtrlJ =
      persistedKey !== undefined && (storage.get(persistedKey) ?? "").includes("Ctrl+J");

    // 2. A FRESH hook mount must honor the persisted binding: the same
    //    Ctrl+J keydown on window opens the shortcuts dialog.
    const bound = renderHook(() => useKeyboardShortcuts());
    act(() => {
      fireEvent.keyDown(window, { key: "j", ctrlKey: true });
    });
    const opened = bound.result.current.open;
    bound.unmount();

    // RED at master: the combo persists (persistedContainsCtrlJ=true) but
    // the hook's ctrl/meta early-return means it never fires (opened=false),
    // so the conjunction is true where it must be false.
    expect(persistedContainsCtrlJ && !opened).toBe(false);
  });

  it("a printable focusSearch combo never steals focus from a textarea", () => {
    // 1. Capture an unmodified "j" for "Focus session search" through the
    //    rebind UI.
    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: focus session search/i })
    );
    act(() => {
      fireEvent.keyDown(window, { key: "j" });
    });
    const persistedKey = Array.from(storage.keys()).find((k) =>
      k.startsWith("kv-keyboard-shortcuts")
    );
    expect(persistedKey, "setup: the focusSearch rebind must persist").toBeDefined();
    const persisted = JSON.parse(storage.get(persistedKey!) ?? "{}") as Record<string, string>;
    expect(persisted.focusSearch, "setup: the persisted combo must be the plain 'j'").toBe("j");

    // 2. Unmount the dialog (its modal focus scope must not fight the
    //    textarea focus below), then render the focusSearch consumer next
    //    to a textarea standing in for the composer.
    cleanup();
    render(
      <div>
        <ChatSearchInput value="" onChange={noop} />
        <textarea aria-label="composer" />
      </div>
    );
    const textarea = screen.getByLabelText("composer");
    textarea.focus();
    expect(document.activeElement, "setup: focus starts on the textarea").toBe(textarea);

    // Typing "j" in the textarea must never move focus away from it.
    fireEvent.keyDown(textarea, { key: "j" });
    expect(document.activeElement === textarea).toBe(true);
  });
});
