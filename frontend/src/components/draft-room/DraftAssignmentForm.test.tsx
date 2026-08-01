import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Vault } from "@/lib/api";
import type { DraftInput } from "@/lib/api/draftRoom";
import {
  DraftAssignmentForm,
  createDefaultDraftAssignmentFormValue,
  type DraftAssignmentFormValue,
} from "./DraftAssignmentForm";
import { MODE_DESCRIPTIONS, TIER_DESCRIPTIONS } from "./labels";

const { mockListAccessibleVaults } = vi.hoisted(() => ({
  mockListAccessibleVaults: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listAccessibleVaults: mockListAccessibleVaults,
}));

// Radix Select cannot be driven in jsdom (no pointer-capture / scrollIntoView).
// Stand in with a plain context-backed mock per the repo's testing gotchas doc.
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
    return React.createElement(SelectCtx.Provider, { value: onValueChange ?? (() => {}) }, children);
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

function makeVault(overrides: Partial<Vault> = {}): Vault {
  return {
    id: 1,
    name: "Research vault",
    description: "",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    file_count: 0,
    memory_count: 0,
    session_count: 0,
    org_id: null,
    effective_enrichment_enabled: false,
    ...overrides,
  };
}

function makeInput(overrides: Partial<DraftInput> = {}): DraftInput {
  return {
    id: 9,
    role: "manuscript",
    authority: "unknown",
    as_of_date: null,
    original_name: "draft.docx",
    extension: ".docx",
    media_type: null,
    size_bytes: 1024,
    content_sha256: "abc123",
    parse_status: "ready",
    parse_error: null,
    parsed_char_count: 500,
    active_parse_job_id: null,
    last_parse_job_id: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderForm(props: Partial<React.ComponentProps<typeof DraftAssignmentForm>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onChange = vi.fn();
  const value = props.value ?? createDefaultDraftAssignmentFormValue();
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <DraftAssignmentForm
        value={value}
        onChange={onChange}
        idPrefix="test"
        {...props}
      />
    </QueryClientProvider>
  );
  return { ...utils, onChange };
}

beforeEach(() => {
  mockListAccessibleVaults.mockReset();
  mockListAccessibleVaults.mockResolvedValue({ vaults: [makeVault()] });
});

describe("DraftAssignmentForm", () => {
  it("shows inline errors and marks the corresponding fields invalid", async () => {
    renderForm({
      errors: {
        title: "Title must be 1-300 characters.",
        "brief.audience": "Audience must be 1-500 characters.",
      },
    });

    const title = await screen.findByLabelText("Project title");
    expect(title).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Title must be 1-300 characters.")).toBeInTheDocument();

    const audience = screen.getByLabelText("Audience");
    expect(audience).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Audience must be 1-500 characters.")).toBeInTheDocument();
  });

  it("shows an honest explanation instead of an empty vault select when none are accessible", async () => {
    mockListAccessibleVaults.mockResolvedValue({ vaults: [] });
    renderForm();

    expect(
      await screen.findByText(/don't have read access to any vault yet/i)
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Vault")).not.toBeInTheDocument();
  });

  it("maps the must_include textarea to a trimmed string[], dropping blank lines", async () => {
    const user = userEvent.setup();
    const { onChange } = renderForm();
    await screen.findByLabelText("Vault");

    const textarea = screen.getByLabelText("Must include (one per line)");
    await user.type(textarea, "  keep the deadline  {enter}{enter}mention the vendor{enter}");

    const lastCall = onChange.mock.calls.at(-1)?.[0] as DraftAssignmentFormValue;
    expect(lastCall.brief.must_include).toEqual(["keep the deadline", "mention the vendor"]);
  });

  it("caps must_avoid at 50 items and shows the cap message", async () => {
    const { onChange } = renderForm();
    await screen.findByLabelText("Vault");

    // Sets the value directly instead of typing 55 lines character by
    // character: the textarea is a plain controlled input (see its onChange
    // in DraftAssignmentForm.tsx), so a single change event exercises the
    // same cap logic without the per-keystroke overhead that made this test
    // flaky under full-suite load (timed out at 5000ms, ~2.9s in isolation).
    const lines = Array.from({ length: 55 }, (_, i) => `line ${i}`).join("\n");
    const textarea = screen.getByLabelText("Must avoid (one per line)");
    fireEvent.change(textarea, { target: { value: lines } });

    expect(screen.getByText(/only the first 50 are kept/i)).toBeInTheDocument();
    const lastCall = onChange.mock.calls.at(-1)?.[0] as DraftAssignmentFormValue;
    expect(lastCall.brief.must_avoid).toHaveLength(50);
  });

  it("selects the project mode via the radio group and renders its help text", async () => {
    const { onChange } = renderForm();
    await screen.findByLabelText("Vault");

    const composeRadio = document.getElementById("test-mode-compose") as HTMLInputElement;
    expect(composeRadio).toBeChecked();
    expect(screen.getByText(MODE_DESCRIPTIONS.compose)).toBeInTheDocument();
    expect(screen.getByText(MODE_DESCRIPTIONS.rewrite)).toBeInTheDocument();

    const rewriteRadio = document.getElementById("test-mode-rewrite") as HTMLInputElement;
    await userEvent.click(rewriteRadio);

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "rewrite" })
    );
  });

  it("shows the tier description for the current value and updates it on selection", async () => {
    const value = { ...createDefaultDraftAssignmentFormValue(), tier: "high_stakes" as const };
    const { onChange } = renderForm({ value });
    await screen.findByLabelText("Vault");

    expect(screen.getByText(TIER_DESCRIPTIONS.high_stakes)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Sensitive" }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ tier: "sensitive" }));
  });

  it("omits title, vault, and mode inputs when variant is edit", async () => {
    renderForm({ variant: "edit" });

    await waitFor(() => {
      expect(screen.getByLabelText("Audience")).toBeInTheDocument();
    });
    expect(screen.queryByLabelText("Project title")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Vault")).not.toBeInTheDocument();
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
    expect(mockListAccessibleVaults).not.toHaveBeenCalled();
  });

  describe("primary source picker", () => {
    it("is absent in the create variant even when inputs are supplied", async () => {
      renderForm({ variant: "create", inputs: [makeInput()] });
      await screen.findByLabelText("Vault");

      expect(screen.queryByLabelText("Primary source")).not.toBeInTheDocument();
    });

    it("is absent in the edit variant when there are no inputs", async () => {
      renderForm({ variant: "edit", inputs: [] });
      await waitFor(() => expect(screen.getByLabelText("Audience")).toBeInTheDocument());

      expect(screen.queryByLabelText("Primary source")).not.toBeInTheDocument();
    });

    it("is present in the edit variant once inputs exist", async () => {
      const inputs = [
        makeInput({ id: 1, original_name: "manuscript.docx", role: "manuscript" }),
        makeInput({ id: 2, original_name: "notes.txt", role: "reference" }),
      ];
      renderForm({ variant: "edit", inputs });

      expect(await screen.findByLabelText("Primary source")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "manuscript.docx (Manuscript)" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "notes.txt (Reference)" })).toBeInTheDocument();
    });

    it("sets primary_input_id when an input is selected", async () => {
      const inputs = [makeInput({ id: 1, original_name: "manuscript.docx", role: "manuscript" })];
      const { onChange } = renderForm({ variant: "edit", inputs });
      await screen.findByLabelText("Primary source");

      await userEvent.click(screen.getByRole("button", { name: "manuscript.docx (Manuscript)" }));

      expect(onChange).toHaveBeenCalledWith(
        expect.objectContaining({
          brief: expect.objectContaining({ primary_input_id: 1 }),
        })
      );
    });

    it("clears primary_input_id to null when None is selected", async () => {
      const inputs = [makeInput({ id: 1, original_name: "manuscript.docx", role: "manuscript" })];
      const base = createDefaultDraftAssignmentFormValue();
      const value = { ...base, brief: { ...base.brief, primary_input_id: 1 } };
      const { onChange } = renderForm({ variant: "edit", inputs, value });
      await screen.findByLabelText("Primary source");

      await userEvent.click(screen.getByRole("button", { name: "None" }));

      expect(onChange).toHaveBeenCalledWith(
        expect.objectContaining({
          brief: expect.objectContaining({ primary_input_id: null }),
        })
      );
    });
  });
});
