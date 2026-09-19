import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ModelsTab } from "@/components/settings/ModelsTab";
import { useSettingsStore } from "@/stores/useSettingsStore";
import type { SettingsFormData } from "@/stores/useSettingsStore";

// Issue #622 / PRR-002 regression: the Models tab is the post-setup key
// management surface — typing rotates (sent via the normal save), the Clear
// control PUTs an explicit empty string, and presence flags drive the
// placeholder + affordance.

function baseFormData(overrides: Partial<SettingsFormData> = {}): SettingsFormData {
  // The store's initial state is the complete canonical form shape — spread
  // it so unrelated ModelsTab fields keep rendering.
  return {
    ...useSettingsStore.getState().formData,
    ...overrides,
  } as SettingsFormData;
}

function renderTab(
  formData: SettingsFormData,
  onClearKey: (field: "chat_api_key" | "instant_api_key") => Promise<void>,
) {
  return render(
    <ModelsTab
      formData={formData}
      errors={{}}
      onChange={vi.fn()}
      effectiveSources={{}}
      onClearKey={onClearKey}
    />
  );
}

describe("ModelsTab API key management (issue #622 / PRR-002)", () => {
  it("hides Clear and uses the empty placeholder when no key is stored", () => {
    renderTab(baseFormData(), vi.fn());
    const input = screen.getByLabelText("Thinking API key");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveValue("");
    expect(
      screen.getByPlaceholderText(/Optional — for remote providers/i)
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
  });

  it("shows the stored-key placeholder and Clear control when a key is set", () => {
    renderTab(
      baseFormData({ chat_api_key_set: true }),
      vi.fn().mockResolvedValue(undefined)
    );
    expect(
      screen.getByPlaceholderText(/Stored — type a new key to replace it/i)
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Clear thinking API key" })
    ).toBeInTheDocument();
  });

  it("Clear sends an explicit empty string for that key only", async () => {
    const user = userEvent.setup();
    const onClearKey = vi.fn().mockResolvedValue(undefined);
    renderTab(
      baseFormData({ chat_api_key_set: true, instant_api_key_set: true }),
      onClearKey
    );

    await user.click(
      screen.getByRole("button", { name: "Clear thinking API key" })
    );

    await waitFor(() => {
      expect(onClearKey).toHaveBeenCalledTimes(1);
    });
    expect(onClearKey).toHaveBeenCalledWith("chat_api_key");
    // The instant key's Clear control stays available and untouched.
    expect(
      screen.getByRole("button", { name: "Clear instant API key" })
    ).toBeInTheDocument();
  });

  it("typing into the key field rotates via the form (onChange with the typed value)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ModelsTab
        formData={baseFormData({ chat_api_key_set: true })}
        errors={{}}
        onChange={onChange}
        effectiveSources={{}}
        onClearKey={vi.fn()}
      />
    );

    await user.type(screen.getByLabelText("Thinking API key"), "sk-new");
    const typed = onChange.mock.calls.filter(([field]) => field === "chat_api_key");
    expect(typed.map(([, value]) => value).join("")).toContain("sk-new");
  });
});
