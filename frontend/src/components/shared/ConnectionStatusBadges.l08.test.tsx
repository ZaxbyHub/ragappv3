import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ConnectionStatusBadges } from "./ConnectionStatusBadges";

// Issue #779 (UI-R3-08): on narrow viewports the three status badges must be
// able to wrap onto multiple lines. A row that is only `flex items-center`
// keeps them on one overflowing line; the acceptance criterion is a
// `flex-wrap` (or equivalent wrap-capable) utility on the row element.
describe("ConnectionStatusBadges row wrapping (issue #779, UI-R3-08)", () => {
  it("badge row wraps instead of overflowing", () => {
    render(
      <ConnectionStatusBadges
        health={{ backend: true, embeddings: true, chat: true, loading: false, lastChecked: null }}
      />
    );

    // Badge's root element is a <span> (components/ui/badge.tsx), so the
    // nearest span ancestor of the label IS the badge; its parent is the row.
    const badge = screen.getByText("Backend").closest("span");
    expect(badge).not.toBeNull();
    const row = badge!.parentElement;
    expect(row).not.toBeNull();

    expect(row!.className.split(/\s+/).includes("flex-wrap")).toBe(true);
  });
});
