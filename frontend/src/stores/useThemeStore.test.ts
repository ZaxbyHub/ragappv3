import { describe, it, expect, beforeEach, vi } from "vitest";

// jsdom does not implement matchMedia, which the store reads at import time and
// inside applyTheme. Stub it before the (dynamic) import below.
beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  document.documentElement.className = "";
  localStorage.clear();
});

describe("useThemeStore — high-contrast", () => {
  it("applies the high-contrast class and clears dark", async () => {
    const { useThemeStore } = await import("./useThemeStore");

    useThemeStore.getState().setTheme("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);

    useThemeStore.getState().setTheme("high-contrast");
    expect(document.documentElement.classList.contains("high-contrast")).toBe(true);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(useThemeStore.getState().theme).toBe("high-contrast");
  });

  it("removes high-contrast when switching to light", async () => {
    const { useThemeStore } = await import("./useThemeStore");

    useThemeStore.getState().setTheme("high-contrast");
    expect(document.documentElement.classList.contains("high-contrast")).toBe(true);

    useThemeStore.getState().setTheme("light");
    expect(document.documentElement.classList.contains("high-contrast")).toBe(false);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });
});
