// frontend/src/tests/command-palette-capability-off.issue775.test.tsx
// Issue #775 review PRR-308(e) — the capability-gated branch of the
// palette's destination filter is exercised: an admin WITHOUT the Draft
// Room capability must not see the /draft-room destination (11 of 12), and
// the exclusion comes from isNavItemVisible's capabilityGated clause, not
// from role gating (every other palette suite mocks the capability ON, so
// C5's member count never exercises this clause palette-side).

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { CommandPalette } from "@/components/shared/CommandPalette";

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      user: { id: 1, username: "alice", role: "admin" },
      logout: vi.fn(),
      init: vi.fn().mockResolvedValue(undefined),
    }),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => false,
}));

vi.mock("@/lib/api/search", () => ({
  unifiedSearch: vi.fn().mockResolvedValue({ results: [] }),
}));

function openPalette(): HTMLElement {
  if (screen.queryByRole("dialog") === null) {
    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });
  }
  return screen.getByRole("dialog");
}

describe("issue #775 review — palette respects the Draft Room capability gate", () => {
  afterEach(() => {
    cleanup();
  });

  it("an admin without the capability sees 11 destinations and no Draft Room entry", () => {
    render(
      <MemoryRouter>
        <CommandPalette />
      </MemoryRouter>
    );
    const palette = openPalette();
    const labels = within(palette)
      .getAllByRole("button")
      .filter((b) => b.textContent?.trim() !== "Close")
      .map((b) => b.textContent ?? "");

    expect(
      within(palette).queryByRole("button", { name: /draft room/i }),
      "capability-gated /draft-room must be excluded when the capability is absent"
    ).toBeNull();
    expect(labels.filter((l) => l.startsWith("Go to ")).length).toBe(11);
  });
});
