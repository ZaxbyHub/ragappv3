// frontend/src/lib/shortcutBindings.roundtrip.issue775.test.ts
// Issue #775 — unit pins for the shared shortcut-combo helpers:
//   1. comboToKeyboardEventInit round-trips through comboFromEvent for every
//      combo shape (modifier chord incl. multi-key "Ctrl+Enter", printable,
//      shifted printable, function key). The palette's "Show keyboard
//      shortcuts" action depends on this round-trip to drive the single
//      existing shortcut dispatcher with the user's bound combo.
//   2. isEditableTarget classifies input/textarea/contentEditable as
//      editable and never classifies window/document (the dispatch targets
//      used by tests and by the palette action).
//   3. The read-time guard ignores a persisted showShortcuts combo in
//      PALETTE_TOGGLE_COMBOS (the stranded state a pre-#775 build could
//      write) while leaving every other persisted binding untouched.
//
// localStorage: in-memory Map layer over the global vi.fn stubs from
// src/test/setup.ts (KeyboardShortcuts.rebind.test.tsx pattern).

import { describe, it, expect, vi, beforeEach } from "vitest";

import {
  comboFromEvent,
  comboToKeyboardEventInit,
  isEditableTarget,
  loadShortcutBindings,
  SHORTCUT_BINDINGS_STORAGE_KEY,
} from "./shortcutBindings";

const ROUND_TRIP_COMBOS = ["Ctrl+K", "Ctrl+J", "Ctrl+Enter", "?", "F9", "j"];

function eventFromInit(init: KeyboardEventInit) {
  return {
    key: init.key as string,
    ctrlKey: Boolean(init.ctrlKey),
    metaKey: Boolean(init.metaKey),
  };
}

describe("issue #775 — shared shortcut-combo helpers", () => {
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
  });

  it("comboToKeyboardEventInit round-trips through comboFromEvent for every combo shape", () => {
    for (const combo of ROUND_TRIP_COMBOS) {
      const init = comboToKeyboardEventInit(combo);
      const normalized = comboFromEvent(eventFromInit(init));
      expect(normalized, `combo ${combo} must round-trip to itself`).toBe(combo);
    }
  });

  it("comboToKeyboardEventInit marks the synthetic event cancelable", () => {
    for (const combo of ROUND_TRIP_COMBOS) {
      expect(comboToKeyboardEventInit(combo).cancelable).toBe(true);
    }
  });

  it("isEditableTarget flags text-entry surfaces and tolerates non-Element targets", () => {
    const input = document.createElement("input");
    const textarea = document.createElement("textarea");
    const plainDiv = document.createElement("div");
    // jsdom does not implement isContentEditable (it reads undefined), so
    // simulate the browser-reported property a real contenteditable surface
    // carries; the predicate reads the DOM property either way.
    const editableDiv = document.createElement("div");
    Object.defineProperty(editableDiv, "isContentEditable", { value: true });

    expect(isEditableTarget(input)).toBe(true);
    expect(isEditableTarget(textarea)).toBe(true);
    expect(isEditableTarget(editableDiv)).toBe(true);
    expect(isEditableTarget(plainDiv)).toBe(false);
    expect(isEditableTarget(window)).toBe(false);
    expect(isEditableTarget(document)).toBe(false);
    expect(isEditableTarget(null)).toBe(false);
  });

  it("the read-time guard drops a stranded showShortcuts reserved combo and nothing else", () => {
    storage.set(
      SHORTCUT_BINDINGS_STORAGE_KEY,
      JSON.stringify({
        showShortcuts: "Ctrl+K", // stranded pre-#775 shape — must be ignored
        focusSearch: "Ctrl+K", // focusSearch CAN fire this on /chat — kept
      })
    );
    expect(loadShortcutBindings()).toEqual({ focusSearch: "Ctrl+K" });

    storage.set(
      SHORTCUT_BINDINGS_STORAGE_KEY,
      JSON.stringify({
        showShortcuts: "F9", // ordinary rebind — kept
        focusSearch: "F6", // ordinary rebind — kept
      })
    );
    expect(loadShortcutBindings()).toEqual({ showShortcuts: "F9", focusSearch: "F6" });
  });
});
