import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { DraftRoomSettings } from "./DraftRoomSettings";
import type { SettingsFormData } from "@/stores/useSettingsStore";

describe("DraftRoomSettings", () => {
  const baseFormData: SettingsFormData = {
    // Minimal shape — only the field this component reads is required.
    draft_room_enabled: false,
  } as Partial<SettingsFormData> as SettingsFormData;

  it("renders unchecked when draft_room_enabled is false", () => {
    render(<DraftRoomSettings formData={baseFormData} onChange={vi.fn()} />);
    const checkbox = screen.getByRole("checkbox", { name: "Enable Draft Room" });
    expect(checkbox).toHaveAttribute("aria-checked", "false");
  });

  it("renders checked when draft_room_enabled is true", () => {
    render(
      <DraftRoomSettings
        formData={{ ...baseFormData, draft_room_enabled: true }}
        onChange={vi.fn()}
      />,
    );
    const checkbox = screen.getByRole("checkbox", { name: "Enable Draft Room" });
    expect(checkbox).toHaveAttribute("aria-checked", "true");
  });

  it("clicking the checkbox calls onChange with the toggled value", () => {
    const onChange = vi.fn();
    render(<DraftRoomSettings formData={baseFormData} onChange={onChange} />);
    const checkbox = screen.getByRole("checkbox", { name: "Enable Draft Room" });
    fireEvent.click(checkbox);
    expect(onChange).toHaveBeenCalledWith("draft_room_enabled", true);
  });
});
