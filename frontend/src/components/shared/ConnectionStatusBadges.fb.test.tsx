import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ConnectionStatusBadges } from "@/components/shared/ConnectionStatusBadges";

describe("ConnectionStatusBadges feedback geometry contract (#779)", () => {
  it("renders all health badges in a right-anchored wrapping row", () => {
    render(
      <ConnectionStatusBadges
        health={{ backend: true, embeddings: true, chat: true, loading: false, lastChecked: null }}
      />,
    );

    const backend = screen.getByText(/Backend/i);
    const row = backend.closest("span")?.parentElement;
    expect(row).not.toBeNull();
    expect(row).toHaveClass("flex", "flex-wrap", "items-center", "justify-end");
    expect(screen.getByText(/Embeddings/i)).toBeVisible();
    expect(screen.getByText(/Chat/i)).toBeVisible();
  });
});
