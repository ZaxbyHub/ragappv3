import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { RetrievalSettings } from "./RetrievalSettings";
import type { SettingsFormData, SettingsErrors } from "@/stores/useSettingsStore";

// WCAG 4.1.2 (Name) — issue #394 LOW-2: the Hybrid Alpha range input must
// expose a programmatic label, because the visible <Label htmlFor> points at
// the paired number input, not the range. This test fails on pre-fix code.
describe("RetrievalSettings hybrid-alpha range a11y", () => {
  const baseFormData: SettingsFormData = {
    // Minimal shape — only the fields this component reads are required.
    hybrid_alpha: 0.5,
  } as Partial<SettingsFormData> as SettingsFormData;
  const baseErrors: SettingsErrors = {};

  it("hybrid alpha range input has an aria-label", () => {
    render(
      <RetrievalSettings
        formData={baseFormData}
        errors={baseErrors}
        onChange={vi.fn()}
      />
    );
    const range = screen.getByRole("slider", { name: "Hybrid Alpha" });
    expect(range).toBeTruthy();
    expect(range.tagName).toBe("INPUT");
    expect((range as HTMLInputElement).type).toBe("range");
  });

  it("hybrid alpha range input reflects the form value", () => {
    render(
      <RetrievalSettings
        formData={{ ...baseFormData, hybrid_alpha: 0.7 }}
        errors={baseErrors}
        onChange={vi.fn()}
      />
    );
    const range = screen.getByRole("slider", { name: "Hybrid Alpha" }) as HTMLInputElement;
    expect(range.value).toBe("0.7");
  });
});
