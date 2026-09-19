// frontend/src/tests/setup-wizard.test.tsx
/**
 * Issue #622 acceptance checks — SetupPage step 2 ("Configure Chat Models").
 *
 * FROZEN SPEC — the fix copies this file VERBATIM to
 * frontend/src/tests/setup-wizard.test.tsx and makes it pass. The
 * assertions encode the interface below; they are not to be edited.
 *
 * Pinned frontend interface (issue #622, gap G1):
 *
 * 1. SetupPage becomes a two-step flow. Step 1 is the existing superadmin
 *    form (unchanged fields/placeholders). After a successful register(),
 *    step 2 renders a section headed exactly "Configure Chat Models" and
 *    the page does NOT navigate yet; navigate("/") happens on finish
 *    (save or skip), as today.
 *
 * 2. Step 2 controls (accessible names / ids are part of the contract):
 *    - Provider preset Select, label "Provider", trigger id
 *      "wizard-provider". Options carry values "ollama" | "lmstudio" |
 *      "vllm" | "other" with labels "Ollama", "LM Studio", "vLLM",
 *      "Other". A preset changes ONLY the Base URL input's placeholder
 *      (an http(s) URL shape) — it never fills a model name.
 *    - Base URL Input, label "Base URL", id "wizard-base-url".
 *    - Model Input, label "Model name", id "wizard-model".
 *    - API key Input, label "API key (optional)", id "wizard-api-key",
 *      type="password".
 *    - "Test connection" Button -> calls probeModelEndpoint.
 *    - "Save and continue" Button -> calls updateSettings.
 *    - "Skip setup" Button -> finishes setup with NO updateSettings call.
 *    - Instant endpoint section: heading text "Instant endpoint
 *      (optional)" with its own "Skip for now" button; its fields (when
 *      rendered) are labeled "Instant base URL" / "Instant model name" /
 *      "Instant API key (optional)" so the required thinking labels above
 *      stay unique in the DOM.
 *
 * 3. Probe status copy (single text runs, rendered inline in step 2):
 *    - ok             -> "Connection successful - the model is available."
 *    - unreachable    -> "Could not reach the endpoint."
 *    - model_mismatch -> "Endpoint reachable, but the model is not served."
 *
 * 4. @/lib/api additions used by SetupPage (pinned):
 *    - probeModelEndpoint({ target, base_url, model, api_key? }) ->
 *      { status: "ok" | "unreachable" | "model_mismatch", detail: string }
 *    - updateSettings payload includes ollama_chat_url + chat_model, plus
 *      chat_api_key when one was entered; it does NOT include
 *      instant_chat_url when the instant section was left untouched.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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
// DraftAssignmentForm.test.tsx) so SelectItem clicks call onValueChange.
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

const STATUS_COPY = {
  ok: "Connection successful - the model is available.",
  unreachable: "Could not reach the endpoint.",
  model_mismatch: "Endpoint reachable, but the model is not served.",
} as const;

const BASE_URL = "http://localhost:11434";
const MODEL = "wizard-model";

function mockStore(register: ReturnType<typeof vi.fn> = vi.fn().mockResolvedValue({ success: true })) {
  vi.spyOn(useAuthStoreModule, "useAuthStore").mockReturnValue({
    register,
    needsSetup: true,
    isLoading: false,
  } as any);
}

async function getNavigateMock() {
  const navigate = vi.fn();
  const { useNavigate } = await import("react-router-dom");
  vi.mocked(useNavigate).mockReturnValue(navigate);
  return navigate;
}

async function completeStepOne(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByPlaceholderText("Username (required)"), "adminuser");
  await user.type(screen.getByPlaceholderText("Password (min 8 characters)"), "securepass123");
  await user.type(screen.getByPlaceholderText("Confirm password"), "securepass123");
  await user.click(screen.getByRole("button", { name: /Create Superadmin Account/i }));
}

async function renderWizard() {
  const user = userEvent.setup();
  const navigate = await getNavigateMock();
  mockStore();
  render(
    <BrowserRouter>
      <SetupPage />
    </BrowserRouter>
  );
  await completeStepOne(user);
  expect(await screen.findByText("Configure Chat Models")).toBeInTheDocument();
  return { user, navigate };
}

async function fillThinkingEndpoint(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Base URL"), BASE_URL);
  await user.type(screen.getByLabelText("Model name"), MODEL);
}

describe("SetupPage step 2 - Configure Chat Models", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUpdateSettings.mockResolvedValue({});
    mockProbeModelEndpoint.mockReset();
  });

  it("shows the wizard only after step 1 completes, and does not navigate yet", async () => {
    const user = userEvent.setup();
    const navigate = await getNavigateMock();
    mockStore();
    render(
      <BrowserRouter>
        <SetupPage />
      </BrowserRouter>
    );

    // Step 2 appears only after a successful register() (gap G1)...
    expect(screen.queryByText("Configure Chat Models")).not.toBeInTheDocument();

    await completeStepOne(user);
    expect(await screen.findByText("Configure Chat Models")).toBeInTheDocument();

    // ...and the page stays on the wizard: no navigate("/") until the
    // operator finishes (save or skip).
    expect(navigate).not.toHaveBeenCalledWith("/");
  });

  it("offers the four provider presets and prefills only a URL-shaped placeholder", async () => {
    const { user } = await renderWizard();

    for (const preset of ["Ollama", "LM Studio", "vLLM", "Other"]) {
      expect(screen.getByRole("button", { name: preset })).toBeInTheDocument();
    }

    await user.click(screen.getByRole("button", { name: "Ollama" }));

    const baseUrl = screen.getByLabelText("Base URL");
    const placeholder = baseUrl.getAttribute("placeholder") ?? "";
    expect(placeholder).toMatch(/^https?:\/\//);
    // Presets never suggest a model name (issue scope item 1).
    expect(screen.getByLabelText("Model name")).toHaveValue("");
  });

  it.each([
    ["ok", STATUS_COPY.ok],
    ["unreachable", STATUS_COPY.unreachable],
    ["model_mismatch", STATUS_COPY.model_mismatch],
  ] as const)(
    "Test connection calls the probe API and renders the %s status inline",
    async (status, copy) => {
      mockProbeModelEndpoint.mockResolvedValue({ status, detail: "stub detail" });
      const { user } = await renderWizard();

      await fillThinkingEndpoint(user);
      await user.click(screen.getByRole("button", { name: "Test connection" }));

      await waitFor(() => {
        expect(mockProbeModelEndpoint).toHaveBeenCalledWith(
          expect.objectContaining({
            target: "thinking",
            base_url: BASE_URL,
            model: MODEL,
          })
        );
      });
      expect(await screen.findByText(copy)).toBeInTheDocument();
    }
  );

  it("Save and continue persists the thinking pair and finishes", async () => {
    const { user, navigate } = await renderWizard();

    await fillThinkingEndpoint(user);
    await user.click(screen.getByRole("button", { name: "Save and continue" }));

    await waitFor(() => {
      expect(mockUpdateSettings).toHaveBeenCalledTimes(1);
    });
    expect(mockUpdateSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        ollama_chat_url: BASE_URL,
        chat_model: MODEL,
      })
    );
    const payload = mockUpdateSettings.mock.calls[0][0] as Record<string, unknown>;
    expect(payload.instant_chat_url).toBeUndefined();
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/");
    });
  });

  it("Save and continue includes the optional API key when one was entered", async () => {
    const { user } = await renderWizard();

    await fillThinkingEndpoint(user);
    await user.type(screen.getByLabelText("API key (optional)"), "sk-wizard-secret");
    await user.click(screen.getByRole("button", { name: "Save and continue" }));

    await waitFor(() => {
      expect(mockUpdateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          ollama_chat_url: BASE_URL,
          chat_model: MODEL,
          chat_api_key: "sk-wizard-secret",
        })
      );
    });
  });

  it("the API key field is masked (type=password)", async () => {
    await renderWizard();
    expect(screen.getByLabelText("API key (optional)")).toHaveAttribute(
      "type",
      "password"
    );
  });

  it("Test connection surfaces a request failure inline (PRR-012)", async () => {
    mockProbeModelEndpoint.mockRejectedValue(new Error("Probe request failed."));
    const { user } = await renderWizard();

    await fillThinkingEndpoint(user);
    await user.click(screen.getByRole("button", { name: "Test connection" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Probe request failed."
    );
    // The status copy renders role="status" — exactly one alert on the page.
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("Skip setup finishes without saving any chat settings", async () => {
    const { user, navigate } = await renderWizard();

    await user.click(screen.getByRole("button", { name: "Skip setup" }));

    expect(mockUpdateSettings).not.toHaveBeenCalled();
    expect(mockProbeModelEndpoint).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/");
    });
  });

  it("renders the optional instant endpoint section with its own Skip for now", async () => {
    await renderWizard();

    expect(
      await screen.findByText("Instant endpoint (optional)")
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skip for now" })).toBeInTheDocument();
  });
});
