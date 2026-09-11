// Regression test for issue #517 acceptance check AC14: brief template
// save/apply affordance in DraftAssignmentForm. Expected to FAIL until the
// fix lands; prints an "AC14 CHECK: FAIL" sentinel immediately before the
// first failing assertion. Mirrors DraftAssignmentForm.test.tsx (ui/select
// jsdom stand-in, vaults api mock).
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Vault } from "@/lib/api";
import {
  DraftAssignmentForm,
  createDefaultDraftAssignmentFormValue,
  type DraftAssignmentFormValue,
} from "./DraftAssignmentForm";

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

function filledValue(): DraftAssignmentFormValue {
  const value = createDefaultDraftAssignmentFormValue(1);
  value.title = "Q3 charter memo";
  value.mode = "rewrite";
  value.tier = "high_stakes";
  value.brief.audience = "Legal reviewers for the board";
  value.brief.purpose = "Explain the internal review window";
  value.brief.tone = "formal and precise";
  value.brief.must_include = ["cite section 4"];
  value.brief.additional_instructions = "Keep the 30-day window explicit.";
  return value;
}

function renderForm(value: DraftAssignmentFormValue, onChange: ReturnType<typeof vi.fn>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DraftAssignmentForm value={value} onChange={onChange} idPrefix="tpl" />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mockListAccessibleVaults.mockReset().mockResolvedValue([makeVault()]);
});

afterEach(() => {
  cleanup();
});

describe("DraftAssignmentForm (issue #517)", () => {
  it("AC14: saves the current brief as a template and reapplies it to a fresh form without bypassing freshness", async () => {
    // Step 1: fill the form and save it as a template.
    const saveChange = vi.fn();
    renderForm(filledValue(), saveChange);

    console.log("AC14 CHECK: FAIL");
    const saveButton = await screen.findByRole("button", { name: /save.*template/i });
    fireEvent.click(saveButton);
    expect(
      saveButton,
      "the form must offer a 'Save as template' affordance"
    ).toBeInTheDocument();

    // Step 2: a fresh form (fresh state, default value) applies the template.
    cleanup();
    const applyChange = vi.fn();
    renderForm(createDefaultDraftAssignmentFormValue(1), applyChange);

    const applyButton = await screen.findByRole("button", { name: /apply.*template|use.*template/i });
    fireEvent.click(applyButton);

    await vi.waitFor(() => expect(applyChange).toHaveBeenCalled());
    const applied = applyChange.mock.calls[0][0] as DraftAssignmentFormValue;
    expect(applied.brief.audience).toBe("Legal reviewers for the board");
    expect(applied.brief.purpose).toBe("Explain the internal review window");
    expect(applied.brief.tone).toBe("formal and precise");
    expect(applied.brief.must_include).toEqual(["cite section 4"]);
    expect(applied.brief.additional_instructions).toBe("Keep the 30-day window explicit.");

    // Applying a template only fills assignment fields: it must not fabricate
    // readiness, fact-check, or approval state of any kind — the applied
    // value carries exactly the form's own field set and nothing else.
    const appliedKeys = Object.keys(applied).sort();
    expect(appliedKeys).toEqual(["brief", "mode", "tier", "title", "vault_id"]);
    expect(
      appliedKeys.some((key) => /fact|ready|approv|checked/i.test(key))
    ).toBe(false);
  });
});
