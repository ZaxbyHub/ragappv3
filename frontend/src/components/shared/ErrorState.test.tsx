import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ErrorState } from "./ErrorState";

describe("ErrorState", () => {
  it("renders default title and description with an alert role", () => {
    render(<ErrorState />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(
      screen.getByText("An unexpected error occurred. Please try again.")
    ).toBeInTheDocument();
  });

  it("renders custom title and description", () => {
    render(<ErrorState title="Failed to load" description="Network error" />);
    expect(screen.getByText("Failed to load")).toBeInTheDocument();
    expect(screen.getByText("Network error")).toBeInTheDocument();
  });

  it("renders a retry action and fires its handler", () => {
    const onClick = vi.fn();
    render(<ErrorState action={{ label: "Try Again", onClick }} />);
    const button = screen.getByRole("button", { name: "Try Again" });
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("renders no action button when none is provided", () => {
    render(<ErrorState />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
