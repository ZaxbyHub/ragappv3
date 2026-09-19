// frontend/src/tests/setup-wizard-a11y.test.tsx
/**
 * Issue #622 acceptance checks — wizard keyboard reachability (AC5).
 *
 * FROZEN SPEC — the fix copies this file VERBATIM to
 * frontend/src/tests/setup-wizard-a11y.test.tsx and makes it pass. The
 * assertions encode the interface below; they are not to be edited.
 *
 * Contract under test (same control names/ids as setup-wizard.test.tsx):
 * every interactive element of the "Configure Chat Models" step is
 * reachable by Tab traversal in DOM order (WCAG 2.4.3 — focus order
 * follows the visual order: provider preset, base URL, model name, API
 * key, Test connection, Save and continue), and every field has an
 * associated label (getByLabelText resolves each one).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter } from "react-router-dom";
import SetupPage from "@/pages/SetupPage";
import * as useAuthStoreModule from "@/stores/useAuthStore";

const { mockProbeModelEndpoint, mockUpdateSettings } = vi.hoisted(() => ({
  mockProbeModelEndpoint: vi.fn(),
  mockUpdateSettings: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  probeModelEndpoint: mockProbeModelEndpoint,
  updateSettings: mockUpdateSettings,
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return {
    ...actual,
    useNavigate: vi.fn(() => vi.fn()),
  };
});

// Radix Select cannot be driven in jsdom (no pointer-capture /
// scrollIntoView). Stand in with a plain context-backed mock per the
// repo's testing gotchas doc (same factory as
// DraftAssignmentForm.test.tsx). The mocked SelectTrigger renders as a
// real <button>, so it participates in Tab traversal exactly like the
// Radix trigger does in a browser.
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const SelectCtx = React.createContext<(v: string) => void>(() => {});

  function Select({
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: (v: string) => void;
    disabled?: boolean;
    children?: React.ReactNode;
  }) {
    return React.createElement(
      SelectCtx.Provider,
      { value: onValueChange ?? (() => {}) },
      children
    );
  }
  function SelectTrigger({
    children,
    ...rest
  }: React.ButtonHTMLAttributes<HTMLButtonElement> & { id?: string }) {
    return React.createElement("button", { type: "button", ...rest }, children);
  }
  function SelectValue() {
    return null;
  }
  function SelectContent({ children }: { children?: React.ReactNode }) {
    return React.createElement("div", null, children);
  }
  function SelectItem({ value, children }: { value: string; children?: React.ReactNode }) {
    const onValueChange = React.useContext(SelectCtx);
    return React.createElement(
      "button",
      { type: "button", onClick: () => onValueChange(value) },
      children
    );
  }
  return {
    Select,
    SelectTrigger,
    SelectValue,
    SelectContent,
    SelectItem,
    SelectGroup: SelectContent,
    SelectLabel: SelectContent,
    SelectSeparator: () => null,
  };
});

async function renderWizardStep() {
  const user = userEvent.setup();
  vi.spyOn(useAuthStoreModule, "useAuthStore").mockReturnValue({
    register: vi.fn().mockResolvedValue({ success: true }),
    needsSetup: true,
    isLoading: false,
  } as any);
  render(
    <BrowserRouter>
      <SetupPage />
    </BrowserRouter>
  );

  await user.type(screen.getByPlaceholderText("Username (required)"), "adminuser");
  await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "securepass123");
  await user.type(screen.getByPlaceholderText("Confirm password"), "securepass123");
  await user.click(screen.getByRole("button", { name: /Create Superadmin Account/i }));
  expect(await screen.findByText("Configure Chat Models")).toBeInTheDocument();
  return user;
}

describe("SetupPage step 2 - keyboard reachability", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUpdateSettings.mockResolvedValue({});
    mockProbeModelEndpoint.mockReset();
  });

  it("associates a label with every wizard field", async () => {
    await renderWizardStep();

    expect(screen.getByLabelText("Provider")).toBeInTheDocument();
    expect(screen.getByLabelText("Base URL")).toBeInTheDocument();
    expect(screen.getByLabelText("Model name")).toBeInTheDocument();
    expect(screen.getByLabelText("API key (optional)")).toBeInTheDocument();
  });

  it("reaches every wizard control via Tab in DOM order", async () => {
    const user = await renderWizardStep();

    const expectedInOrder = [
      screen.getByLabelText("Provider"),
      screen.getByLabelText("Base URL"),
      screen.getByLabelText("Model name"),
      screen.getByLabelText("API key (optional)"),
      screen.getByRole("button", { name: "Test connection" }),
      screen.getByRole("button", { name: "Save and continue" }),
    ];
    const mustBeReachable = [
      ...expectedInOrder,
      screen.getByRole("button", { name: "Skip setup" }),
      screen.getByRole("button", { name: "Skip for now" }),
    ];

    const focused: Element[] = [];
    let guard = 0;
    while (guard < 60) {
      guard += 1;
      await user.tab();
      const el = document.activeElement;
      if (!el || el === document.body) break;
      focused.push(el);
    }

    const missing = mustBeReachable.filter((el) => !focused.includes(el));
    expect(
      missing,
      `controls not reachable by keyboard: ${missing.length}`
    ).toHaveLength(0);

    const indices = expectedInOrder.map((el) => focused.indexOf(el));
    for (let i = 0; i < indices.length - 1; i += 1) {
      expect(
        indices[i],
        `focus order broken at position ${i}: expected DOM order provider -> base URL -> model -> API key -> Test connection -> Save`
      ).toBeLessThan(indices[i + 1]);
    }
  });
});
