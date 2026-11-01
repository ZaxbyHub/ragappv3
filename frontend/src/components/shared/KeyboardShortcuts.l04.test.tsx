// Issue #775 AC2/AC3: rebind capture must either reject an unusable combo or
// prove that the fresh real consumer fires it. Printable focus-search bindings
// must respect editable targets and still focus their real noneditable owner.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";

import { ChatSearchInput } from "@/components/chat/SessionRail";
import {
  KeyboardShortcutsDialog,
  useKeyboardShortcuts,
} from "@/components/shared/KeyboardShortcuts";
import { SHORTCUT_BINDINGS_STORAGE_KEY } from "@/lib/shortcutBindings";

function readBindings(): Record<string, string> {
  const raw = window.localStorage.getItem(SHORTCUT_BINDINGS_STORAGE_KEY);
  return raw ? (JSON.parse(raw) as Record<string, string>) : {};
}

describe("#775 rebind validity", () => {
  const storage = new Map<string, string>();

  beforeEach(() => {
    storage.clear();
    vi.mocked(window.localStorage.getItem).mockImplementation(
      (key: string) => storage.get(key) ?? null
    );
    vi.mocked(window.localStorage.setItem).mockImplementation(
      (key: string, value: string) => {
        storage.set(key, value);
      }
    );
    vi.mocked(window.localStorage.removeItem).mockImplementation((key: string) => {
      storage.delete(key);
    });
    vi.mocked(window.localStorage.clear).mockImplementation(() => {
      storage.clear();
    });
  });

  afterEach(() => {
    cleanup();
    storage.clear();
  });

  it("does not leave Show keyboard shortcuts on a combo that cannot fire", () => {
    window.localStorage.setItem(
      SHORTCUT_BINDINGS_STORAGE_KEY,
      JSON.stringify({ showShortcuts: "?" })
    );
    render(<KeyboardShortcutsDialog open={true} onOpenChange={() => undefined} />);
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: show keyboard shortcuts/i })
    );
    fireEvent.keyDown(window, { key: "j", code: "KeyJ", ctrlKey: true });

    const persisted = readBindings().showShortcuts;
    cleanup();
    const freshHook = renderHook(() => useKeyboardShortcuts());
    fireEvent.keyDown(document.body, { key: "j", code: "KeyJ", ctrlKey: true });

    if (persisted === "Ctrl+J") {
      expect(freshHook.result.current.open).toBe(true);
    } else {
      // A refusal must be observable in storage and must preserve the shipped
      // binding, rather than silently clearing the shortcut.
      expect(persisted).toBe("?");
      fireEvent.keyDown(document.body, { key: "?", code: "Slash", shiftKey: true });
      expect(freshHook.result.current.open).toBe(true);
    }
  });

  it("accepts printable focus search only with editable guards and a real owner", async () => {
    window.localStorage.setItem(
      SHORTCUT_BINDINGS_STORAGE_KEY,
      JSON.stringify({ focusSearch: "Ctrl+K" })
    );
    render(<KeyboardShortcutsDialog open={true} onOpenChange={() => undefined} />);
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: focus session search/i })
    );
    fireEvent.keyDown(window, { key: "j", code: "KeyJ" });

    const persisted = readBindings().focusSearch;
    if (persisted !== "j") {
      // The issue permits refusal. Preserve the previous default and make the
      // refusal observable instead of asserting a fabricated persistence path.
      expect(persisted).toBe("Ctrl+K");
      expect(screen.queryByTestId("capture-hint")).toBeNull();
      return;
    }

    // Close the capture dialog before typing into the consumer. Otherwise the
    // modal's focus trap would hide whether the textarea itself stayed owner.
    cleanup();
    render(
      <>
        <textarea aria-label="Chat composer" />
        <button type="button" aria-label="Noneditable owner">
          Owner
        </button>
        <ChatSearchInput value="" onChange={() => undefined} />
      </>
    );

    const textarea = screen.getByRole("textbox", { name: "Chat composer" });
    const search = screen.getByRole("textbox", { name: "Search chat sessions" });
    textarea.focus();
    fireEvent.keyDown(textarea, { key: "j", code: "KeyJ" });
    expect(document.activeElement).toBe(textarea);

    const noneditableOwner = screen.getByRole("button", { name: "Noneditable owner" });
    noneditableOwner.focus();
    fireEvent.keyDown(noneditableOwner, { key: "j", code: "KeyJ" });
    await vi.waitFor(() => expect(document.activeElement).toBe(search));
  });
});
