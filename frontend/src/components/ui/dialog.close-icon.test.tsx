import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "./dialog";

// WCAG 1.4.2 (Audio Control) / decorative-icon handling — issue #394 LOW-3:
// the dialog close icon is decorative (the button has an sr-only "Close"
// text). The HugeiconsIcon SVG must carry aria-hidden so screen readers do
// not double-announce. This test fails on pre-fix code (icon had no aria-hidden).
describe("Dialog close icon a11y", () => {
  it("close-button HugeiconsIcon is marked aria-hidden", () => {
    render(
      <Dialog defaultOpen>
        <DialogContent>
          <DialogTitle>Test dialog</DialogTitle>
          <p>body</p>
        </DialogContent>
      </Dialog>
    );

    const closeButton = screen.getByRole("button", { name: "Close" });
    expect(closeButton).toBeTruthy();

    // The decorative SVG inside the close button must be hidden from AT.
    const svg = closeButton.querySelector("svg");
    expect(svg).toBeTruthy();
    expect(svg?.getAttribute("aria-hidden")).toBe("true");
  });
});
