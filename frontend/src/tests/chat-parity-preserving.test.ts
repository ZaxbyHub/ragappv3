// frontend/src/tests/chat-parity-preserving.test.ts
// Issue #573 (AC8) — acceptance check C8 (PRESERVING): the already-shipped
// export + overlay parity surfaces must stay single. Expected GREEN at base
// commit ae2e15a0; any RED here is a regression introduced by the #573
// implementation, not a pre-existing gap.
//
// Source-scan guardrails (fonts-selfhosted-assets.test.ts pattern):
//   1. handleExportChat is defined exactly once (ChatShell.tsx) and is still
//      wired to the header export button.
//   2. Exactly one "Export chat" button exists across non-test source.
//   3. Exactly one markdown-export mechanism: exactly one text/markdown Blob
//      construction in non-test source (ChatShell's handleExportChat).
//   4. KeyboardShortcutsDialog is still exported from KeyboardShortcuts.tsx
//      and still mounted exactly once in non-test source. Since issue #775
//      the single mount lives at the app shell (App.tsx AppShortcutsMount)
//      instead of ChatShell — the dialog is now reachable on every route;
//      the chat-page behavior this guardrail protected is preserved through
//      the shell mount.

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

const FRONTEND_SRC = resolve(__dirname, "..");
const CHAT_SHELL = resolve(FRONTEND_SRC, "pages/ChatShell.tsx");
const APP = resolve(FRONTEND_SRC, "App.tsx");
const KEYBOARD_SHORTCUTS = resolve(FRONTEND_SRC, "components/shared/KeyboardShortcuts.tsx");

function countMatches(haystack: string, pattern: RegExp): number {
  const matches = haystack.match(new RegExp(pattern.source, pattern.flags.includes("g") ? pattern.flags : pattern.flags + "g"));
  return matches ? matches.length : 0;
}

function listSourceFiles(dir: string, acc: string[] = []): string[] {
  // Minimal recursive walker — test files and __tests__ dirs excluded so the
  // scan measures production source only.
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith(".") || entry.name === "__tests__" || entry.name === "fixtures") continue;
    const full = resolve(dir, entry.name);
    if (entry.isDirectory()) {
      listSourceFiles(full, acc);
    } else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name)) {
      acc.push(full);
    }
  }
  return acc;
}

describe("chat UI parity — preserving shipped surfaces (issue #573 AC8 / C8)", () => {
  it("handleExportChat is defined exactly once and still wired to the export button", () => {
    const shell = readFileSync(CHAT_SHELL, "utf-8");

    expect(
      countMatches(shell, /const handleExportChat\s*=/),
      "handleExportChat must be defined exactly once in ChatShell.tsx (issue #573 AC8: export stays single)"
    ).toBe(1);
    expect(shell).toContain("onClick={handleExportChat}");
  });

  it("exactly one 'Export chat' button exists across non-test frontend source", () => {
    const files = listSourceFiles(FRONTEND_SRC);
    let total = 0;
    const hits: string[] = [];
    for (const file of files) {
      const n = countMatches(readFileSync(file, "utf-8"), /aria-label="Export chat"/);
      if (n > 0) {
        total += n;
        hits.push(file);
      }
    }
    expect(
      total,
      `expected exactly one 'Export chat' button, found ${total} in: ${hits.join(", ")} (issue #573 AC8: no second export surface)`
    ).toBe(1);
    expect(hits[0].endsWith("ChatShell.tsx")).toBe(true);
  });

  it("exactly one markdown-export mechanism: one text/markdown Blob in non-test source", () => {
    const files = listSourceFiles(FRONTEND_SRC);
    const hits: string[] = [];
    for (const file of files) {
      const source = readFileSync(file, "utf-8");
      if (/new Blob\([^)]*\)\s*;?/.test(source) && source.includes("text/markdown")) {
        hits.push(file);
      }
    }
    expect(
      hits.length,
      `the markdown (.md download) export must exist in exactly one source file, found: ${hits.join(", ")} (issue #573 AC8: the new Share action is a clipboard link, NOT a second markdown export)`
    ).toBe(1);
    expect(hits[0].endsWith("ChatShell.tsx")).toBe(true);
  });

  it("KeyboardShortcutsDialog is still exported from KeyboardShortcuts.tsx", () => {
    const source = readFileSync(KEYBOARD_SHORTCUTS, "utf-8");
    expect(source).toMatch(/export function KeyboardShortcutsDialog/);
  });

  it("KeyboardShortcutsDialog is mounted exactly once, at the app shell", () => {
    // Issue #775 contract evolution: the dialog moved from ChatShell to the
    // app shell (App.tsx AppShortcutsMount) so "?" and the palette's
    // shortcuts action work on every route. The guardrail's intent — the
    // dialog is mounted exactly once and the chat page keeps it — holds.
    const files = listSourceFiles(FRONTEND_SRC);
    const hits: string[] = [];
    for (const file of files) {
      if (countMatches(readFileSync(file, "utf-8"), /<KeyboardShortcutsDialog/) > 0) {
        hits.push(file);
      }
    }
    expect(
      hits.length,
      `KeyboardShortcutsDialog must be mounted in exactly one non-test source file, found: ${hits.join(", ")} (issue #573 AC8 / #775)`
    ).toBe(1);
    expect(hits[0].endsWith("App.tsx")).toBe(true);
    expect(readFileSync(APP, "utf-8")).toContain("AppShortcutsMount");
    expect(readFileSync(CHAT_SHELL, "utf-8")).not.toContain("<KeyboardShortcutsDialog");
  });
});
