import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import { resolve } from "path";

// Source-inspection test (sanctioned by docs/engineering/testing.md for
// cross-cutting structural invariants that are disproportionately expensive
// to exercise behaviorally). ChatShell pulls in ~10 heavyweight dependencies
// (SSE, chat stores, test-mode fixtures) that make a full render brittle;
// the load-bearing invariant here is that VaultSelector is imported and
// rendered in the header JSX. A regression that removed it would fail this
// test. (C2-10: previously titled "Integration" but never rendered — renamed
// and documented honestly as structural inspection.)
describe("ChatShell VaultSelector (structural inspection)", () => {
  const chatShellPath = resolve(__dirname, "./ChatShell.tsx");
  const chatShellContent = readFileSync(chatShellPath, "utf-8");

  it("imports VaultSelector from @/components/vault/VaultSelector", () => {
    expect(
      chatShellContent.includes(
        'import { VaultSelector } from "@/components/vault/VaultSelector"'
      )
    ).toBe(true);
  });

  it("renders <VaultSelector /> in the header JSX (between title and export button)", () => {
    // Extract the header block and assert VaultSelector is present in order.
    const headerSectionMatch = chatShellContent.match(
      /<header[^>]*>[\s\S]*?<\/header>/
    );
    expect(headerSectionMatch).not.toBeNull();
    const headerContent = headerSectionMatch![0];
    expect(headerContent).toContain("<VaultSelector");

    const titleIndex = headerContent.indexOf("activeSessionTitle");
    const vaultIndex = headerContent.indexOf("<VaultSelector");
    const downloadIndex = headerContent.indexOf("<Download");
    expect(vaultIndex).toBeGreaterThan(titleIndex);
    expect(vaultIndex).toBeLessThan(downloadIndex);
  });

  it("keeps the session-rail and details-panel toggles at the outer edges of the header", () => {
    // Regression guard: the header's relative element order must remain
    // PanelLeft < title < VaultSelector < Download < PanelRight. A reorder
    // of the sidebar-toggle icons around VaultSelector/export would not be
    // caught by the "between title and export button" check above alone.
    const headerSectionMatch = chatShellContent.match(
      /<header[^>]*>[\s\S]*?<\/header>/
    );
    expect(headerSectionMatch).not.toBeNull();
    const headerContent = headerSectionMatch![0];

    const panelLeftIndex = headerContent.indexOf("<PanelLeft");
    const titleIndex = headerContent.indexOf("activeSessionTitle");
    const vaultIndex = headerContent.indexOf("<VaultSelector");
    const downloadIndex = headerContent.indexOf("<Download");
    const panelRightIndex = headerContent.indexOf("<PanelRight");

    expect(panelLeftIndex).toBeGreaterThan(-1);
    expect(panelRightIndex).toBeGreaterThan(-1);
    expect(panelLeftIndex).toBeLessThan(titleIndex);
    expect(titleIndex).toBeLessThan(vaultIndex);
    expect(vaultIndex).toBeLessThan(downloadIndex);
    expect(downloadIndex).toBeLessThan(panelRightIndex);
  });
});
