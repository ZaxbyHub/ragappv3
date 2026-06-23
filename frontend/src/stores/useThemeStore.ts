import { create } from "zustand";
import { persist } from "zustand/middleware";

export type Theme = "light" | "dark" | "system" | "high-contrast";

interface ThemeState {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  const isDark =
    theme === "dark" ||
    (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);

  // High contrast is a light-based theme; ensure dark is off so the two
  // token blocks never stack.
  root.classList.toggle("dark", isDark);
  root.classList.toggle("high-contrast", theme === "high-contrast");
}

export const useThemeStore = create<ThemeState>()(
  persist(
    (set) => ({
      theme: "system",
      setTheme: (theme: Theme) => {
        applyTheme(theme);
        set({ theme });
      },
    }),
    { name: "kv-theme" }
  )
);

// Apply theme on load
applyTheme(useThemeStore.getState().theme);

// Listen for system preference changes when in "system" mode
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (useThemeStore.getState().theme === "system") {
    applyTheme("system");
  }
});
