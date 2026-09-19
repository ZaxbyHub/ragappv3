// frontend/src/components/chat/SessionRail.rebind.test.tsx
// Issue #573 (AC4, plan-critic REV-3): the SessionRail search-focus consumer
// (ChatSearchInput's window listener, default Ctrl/Cmd+K) must honor the
// persisted per-browser rebinding from @/lib/shortcutBindings — the frozen C4
// spec pins the dialog-open combo; this file pins the second consumer.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";

import { ChatSearchInput } from "./SessionRail";
import { SHORTCUT_BINDINGS_STORAGE_KEY } from "@/lib/shortcutBindings";

function searchInput(): HTMLInputElement {
  const el = document.querySelector('input[aria-label="Search chat sessions"]');
  if (!(el instanceof HTMLInputElement)) throw new Error("search input not rendered");
  return el;
}

describe("SessionRail search-focus shortcut rebinding (issue #573 AC4)", () => {
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

  afterEach(() => {
    cleanup();
  });

  it("default binding: Ctrl+K focuses the session search", () => {
    render(<ChatSearchInput value="" onChange={vi.fn()} />);
    (document.activeElement as HTMLElement | null)?.blur();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    expect(document.activeElement).toBe(searchInput());
  });

  it("persisted rebinding: the custom combo focuses and the default no longer does", () => {
    storage.set(
      SHORTCUT_BINDINGS_STORAGE_KEY,
      JSON.stringify({ focusSearch: "F6" })
    );
    render(<ChatSearchInput value="" onChange={vi.fn()} />);

    (document.activeElement as HTMLElement | null)?.blur();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    expect(
      document.activeElement,
      "the default Ctrl+K must stand down while a custom focus-search binding is persisted"
    ).not.toBe(searchInput());

    fireEvent.keyDown(document, { key: "F6" });
    expect(
      document.activeElement,
      "the persisted combo must focus the session search"
    ).toBe(searchInput());
  });
});
