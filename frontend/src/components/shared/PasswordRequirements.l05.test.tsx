import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { PasswordRequirements } from "./PasswordRequirements";

/**
 * Issue #776 (UI-ENH-12 / UI-R4-13): one shared statement of the server's
 * password rules. The three rules must match backend password_strength_check
 * (see backend/tests/test_l05_password_rule_contract.py).
 */
describe("PasswordRequirements (issue #776 shared component)", () => {
  it("states all three server rules in static mode", () => {
    render(<PasswordRequirements />);
    const text = document.body.textContent ?? "";
    expect(/at least 8 characters/i.test(text)).toBe(true);
    expect(/digit/i.test(text)).toBe(true);
    expect(/uppercase/i.test(text)).toBe(true);
  });

  it("toggles each live-mode rule with the input value", () => {
    const { rerender } = render(<PasswordRequirements value="short" />);
    expect(screen.getAllByText("requirement not met")).toHaveLength(3);

    rerender(<PasswordRequirements value="longenough1" />);
    const met = screen.getAllByText("requirement met");
    expect(met).toHaveLength(2); // length + digit, uppercase still missing

    rerender(<PasswordRequirements value="Longenough1" />);
    expect(screen.getAllByText("requirement met")).toHaveLength(3);
  });

  it("keeps the aria-describedby id on both modes", () => {
    const { rerender } = render(<PasswordRequirements id="password-requirements" />);
    expect(document.getElementById("password-requirements")?.tagName).toBe("P");
    rerender(<PasswordRequirements id="password-requirements" value="x" />);
    expect(document.getElementById("password-requirements")?.tagName).toBe("UL");
  });
});
