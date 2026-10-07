// frontend/src/components/shared/KeyboardShortcuts.shadowed-capture.issue775.test.tsx
// Issue #775 — rebind-capture safety nets beyond the frozen C2/C3 checks:
//
//   1. Shadow refusal: capturing Ctrl+K for showShortcuts is REFUSED (the
//      combo is claimed on /chat by focusSearch's default and everywhere
//      else by the palette's reserved toggle — it can never fire). Nothing
//      is persisted for showShortcuts, focusSearch's own persisted binding
//      is left untouched, and the capture stays armed.
//   2. Enter bound to focusSearch (a non-printable editing key) never steals
//      focus from a focused textarea — the frozen C3 check pins only the
//      printable "j" case.
//   3. Stranded state: a pre-existing persisted {"showShortcuts":"Ctrl+K"}
//      (writable by pre-#775 builds) is ignored at read time, so the default
//      "?" still opens the dialog.
//   4. Clause-1 generality pin: with focusSearch persisted to "F7", capturing
//      "F7" for showShortcuts is an ALLOWED steal — showShortcuts ends up
//      with "F7" (and fires), focusSearch reverts to its Ctrl+K default.
//      Pins the general post-clear shadow predicate against collapsing to a
//      hard-coded "Ctrl+K" check.
//
// Harness: the rebind-flow pattern from KeyboardShortcuts.rebind.test.tsx
// (localStorage Map layer, dialog rebind button, window keydown) with static
// imports + hoisted mocks only.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, cleanup, renderHook } from "@testing-library/react";

import {
  useKeyboardShortcuts,
  KeyboardShortcutsDialog,
  bindingFor,
} from "./KeyboardShortcuts";
import { ChatSearchInput } from "@/components/chat/SessionRail";
import {
  loadShortcutBindings,
  SHORTCUT_BINDINGS_STORAGE_KEY,
} from "@/lib/shortcutBindings";

const noop = () => {};

const isOpen = (result: { current: { open: boolean } }) => result.current.open;

function seedStorage(bindings: Record<string, string>) {
  window.localStorage.setItem(SHORTCUT_BINDINGS_STORAGE_KEY, JSON.stringify(bindings));
}

function countFocus(el: HTMLElement): () => number {
  let n = 0;
  el.addEventListener("focus", () => {
    n += 1;
  });
  return () => n;
}

describe("issue #775 — rebind capture safety nets", () => {
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

  it("capture of the palette-reserved Ctrl+K for showShortcuts is refused and side-effect free", () => {
    // focusSearch holds a persisted override the refusal must NOT clear.
    seedStorage({ focusSearch: "F6" });

    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: show keyboard shortcuts/i })
    );
    expect(screen.getByText(/press/i)).toBeInTheDocument();

    act(() => {
      fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    });

    // Refused: nothing persisted for showShortcuts, capture still armed.
    const persisted = storage.get(SHORTCUT_BINDINGS_STORAGE_KEY) ?? "";
    expect(persisted.includes("showShortcuts")).toBe(false);
    expect(screen.getByText(/press/i)).toBeInTheDocument();
    // Side-effect free: focusSearch's persisted F6 survives the refusal.
    expect(loadShortcutBindings()).toEqual({ focusSearch: "F6" });
  });

  it("an Enter binding for focusSearch never steals focus from a textarea", () => {
    // Capture "Enter" for focusSearch through the dialog.
    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: focus session search/i })
    );
    act(() => {
      fireEvent.keyDown(window, { key: "Enter" });
    });
    expect(loadShortcutBindings()).toEqual({ focusSearch: "Enter" });
    cleanup();

    // ChatSearchInput + a focused textarea; pressing Enter mid-edit must
    // keep focus in the textarea.
    render(
      <div>
        <ChatSearchInput value="" onChange={() => {}} />
        <textarea aria-label="composer" />
      </div>
    );
    const textarea = screen.getByLabelText("composer") as HTMLTextAreaElement;
    textarea.focus();
    expect(document.activeElement).toBe(textarea);

    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(document.activeElement).toBe(textarea);
  });

  it("a pre-existing stranded showShortcuts Ctrl+K binding falls back to the default ?", () => {
    seedStorage({ showShortcuts: "Ctrl+K" });

    const stranded = renderHook(() => useKeyboardShortcuts());
    expect(isOpen(stranded.result)).toBe(false);
    // The read-time guard ignores the dead entry, so the default still fires.
    expect(bindingFor("showShortcuts")).toBe("?");
    act(() => {
      fireEvent.keyDown(window, { key: "?", shiftKey: true, ctrlKey: false, metaKey: false });
    });
    expect(isOpen(stranded.result)).toBe(true);
    stranded.unmount();
  });

  it("a persisted focusSearch combo remains stealable for showShortcuts (clause-1 generality)", () => {
    seedStorage({ focusSearch: "F7" });

    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: show keyboard shortcuts/i })
    );
    act(() => {
      fireEvent.keyDown(window, { key: "F7" });
    });

    // Allowed steal: showShortcuts holds F7; focusSearch reverts to default.
    expect(loadShortcutBindings()).toEqual({ showShortcuts: "F7" });

    // And the stolen combo actually fires from a fresh hook mount.
    const rebound = renderHook(() => useKeyboardShortcuts());
    act(() => {
      fireEvent.keyDown(window, { key: "F7" });
    });
    expect(isOpen(rebound.result)).toBe(true);
    rebound.unmount();
  });

  it("an ordinary Ctrl-chord rebind for showShortcuts persists AND fires (chosen semantics, not capture refusal)", () => {
    // Pins the OTHER half of the C2 contract that the frozen check leaves
    // open (a capture-refusal fix also passes the frozen assertion): this
    // implementation's choice is that unshadowed chords are persisted and
    // the consumer fires them.
    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: show keyboard shortcuts/i })
    );
    act(() => {
      fireEvent.keyDown(window, { key: "j", ctrlKey: true });
    });
    expect(loadShortcutBindings()).toEqual({ showShortcuts: "Ctrl+J" });
    cleanup();

    const rebound = renderHook(() => useKeyboardShortcuts());
    act(() => {
      fireEvent.keyDown(window, { key: "j", ctrlKey: true });
    });
    expect(isOpen(rebound.result)).toBe(true);
    rebound.unmount();
  });

  it("the reverse steal — capturing '?' for focusSearch — is refused (PRR-401)", () => {
    // Generalized shadow refusal: "?" is showShortcuts' immovable default,
    // so persisting it for focusSearch would strand the dialog on /chat.
    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: focus session search/i })
    );
    expect(screen.getByText(/press/i)).toBeInTheDocument();

    act(() => {
      fireEvent.keyDown(window, { key: "?" });
    });

    expect(
      loadShortcutBindings(),
      "a refused capture must persist nothing"
    ).toEqual({});
    expect(
      screen.getByText(/press/i),
      "capture stays armed like a bare modifier press"
    ).toBeInTheDocument();
  });

  it("focusSearch re-capturing its own default Ctrl+K is an allowed identity rebind (delta review r1)", () => {
    // The reserved-combo clause is showShortcuts-scoped: focusSearch IS the
    // /chat claimant of Ctrl+K, so persisting its own default fires fine and
    // must not be silently refused (delta-review round 1 regression catch).
    render(<KeyboardShortcutsDialog open={true} onOpenChange={noop} />);
    fireEvent.click(
      screen.getByRole("button", { name: /rebind shortcut: focus session search/i })
    );
    act(() => {
      fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    });

    expect(loadShortcutBindings()).toEqual({ focusSearch: "Ctrl+K" });
    expect(
      screen.queryByText(/press/i),
      "the capture completed (not refused)"
    ).toBeNull();
  });

  it("an IME-composing keydown never opens the dialog (PRR-308a)", () => {
    const hooked = renderHook(() => useKeyboardShortcuts());
    expect(isOpen(hooked.result)).toBe(false);

    act(() => {
      // isComposing is not constructible via KeyboardEventInit — define it
      // on the instance exactly as a real IME-driven dispatch reports it.
      const e = new KeyboardEvent("keydown", {
        key: "?",
        shiftKey: true,
        cancelable: true,
      });
      Object.defineProperty(e, "isComposing", { value: true });
      window.dispatchEvent(e);
    });
    expect(isOpen(hooked.result)).toBe(false);
    hooked.unmount();
  });

  it("a defaultPrevented keydown never opens the dialog (PRR-308b)", () => {
    const hooked = renderHook(() => useKeyboardShortcuts());
    expect(isOpen(hooked.result)).toBe(false);

    act(() => {
      const e = new KeyboardEvent("keydown", {
        key: "?",
        shiftKey: true,
        cancelable: true,
      });
      e.preventDefault();
      window.dispatchEvent(e);
    });
    expect(isOpen(hooked.result), "an earlier claimant must own the combo").toBe(false);
    hooked.unmount();
  });

  it("Ctrl+K still focuses search from INSIDE the composer (preserved carve-out, PRR-308c)", () => {
    render(
      <div>
        <ChatSearchInput value="" onChange={() => {}} />
        <textarea aria-label="composer" />
      </div>
    );
    const textarea = screen.getByLabelText("composer") as HTMLTextAreaElement;
    const searchInput = screen.getByLabelText("Search chat sessions");
    const searchFocus = countFocus(searchInput);
    textarea.focus();
    expect(document.activeElement).toBe(textarea);

    fireEvent.keyDown(textarea, { key: "k", ctrlKey: true });

    expect(
      searchFocus(),
      "modifier combos keep focusing search from the composer (advertised behavior)"
    ).toBe(1);
    expect(document.activeElement).toBe(searchInput);
  });
});
