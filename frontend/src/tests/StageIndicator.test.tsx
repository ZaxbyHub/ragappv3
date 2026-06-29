/**
 * FR-015: StageIndicator — renders the correct label per stage and is hidden when null.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { StageIndicator } from "@/components/chat/StageIndicator";

describe("StageIndicator", () => {
  it("renders Searching stage with correct label", () => {
    render(<StageIndicator stage="Searching" />);
    expect(screen.getByRole("status")).toHaveAttribute("aria-label", "Pipeline stage: Searching");
    expect(screen.getByText("Searching")).toBeInTheDocument();
  });

  it("renders Reading stage with correct label", () => {
    render(<StageIndicator stage="Reading" />);
    expect(screen.getByRole("status")).toHaveAttribute("aria-label", "Pipeline stage: Reading");
    expect(screen.getByText("Reading")).toBeInTheDocument();
  });

  it("renders Drafting stage with correct label", () => {
    render(<StageIndicator stage="Drafting" />);
    expect(screen.getByRole("status")).toHaveAttribute("aria-label", "Pipeline stage: Drafting");
    expect(screen.getByText("Drafting")).toBeInTheDocument();
  });

  it("returns null and renders nothing for unknown stage", () => {
    const { container } = render(<StageIndicator stage="UnknownStage" as any />);
    expect(container).toBeEmptyDOMElement();
  });
});
