import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Progress } from "./progress";

describe("Progress", () => {
  it("accepts and passes aria-label to DOM", () => {
    render(<Progress value={50} aria-label="Upload progress" />);
    const progressBar = screen.getByRole("progressbar");
    expect(progressBar).toHaveAttribute("aria-label", "Upload progress");
  });

  it("renders with Radix primitive", () => {
    render(<Progress value={50} />);
    const progressBar = screen.getByRole("progressbar");
    expect(progressBar).toBeInTheDocument();
  });

  it("forwards the numeric value to the progressbar role (aria-valuenow)", () => {
    render(<Progress value={45} aria-label="Upload progress" />);
    const progressBar = screen.getByRole("progressbar");
    expect(progressBar).toHaveAttribute("aria-valuenow", "45");
    expect(progressBar).toHaveAttribute("aria-valuemin", "0");
    expect(progressBar).toHaveAttribute("aria-valuemax", "100");
  });

  it("renders value 0 as a determinate 0, not indeterminate", () => {
    render(<Progress value={0} aria-label="Upload progress" />);
    const progressBar = screen.getByRole("progressbar");
    expect(progressBar).toHaveAttribute("aria-valuenow", "0");
    expect(progressBar).not.toHaveAttribute("data-state", "indeterminate");
  });

  it("reflects a determinate complete state at value 100", () => {
    render(<Progress value={100} aria-label="Upload progress" />);
    const progressBar = screen.getByRole("progressbar");
    expect(progressBar).toHaveAttribute("aria-valuenow", "100");
    expect(progressBar).toHaveAttribute("data-state", "complete");
  });

  it("stays indeterminate (no aria-valuenow) when no value is given", () => {
    render(<Progress aria-label="Upload progress" />);
    const progressBar = screen.getByRole("progressbar");
    expect(progressBar).not.toHaveAttribute("aria-valuenow");
    expect(progressBar).toHaveAttribute("data-state", "indeterminate");
  });
});
