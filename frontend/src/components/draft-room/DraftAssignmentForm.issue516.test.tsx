// Regression test for issue #516 acceptance check AC20 (UI-022 assignment
// list resync). Mirrors DraftAssignmentForm.test.tsx (mocked @/lib/api,
// context-backed ui/select stub, QueryClientProvider, fireEvent on the plain
// controlled textareas). This test asserts REQUIRED behavior and is expected
// to FAIL until the fix lands; it prints an AC<n> CHECK sentinel.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useEffect, useState } from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Vault } from "@/lib/api";
import type { DraftBrief, DraftInput } from "@/lib/api/draftRoom";
import {
  DraftAssignmentForm,
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

function makeBrief(overrides: Partial<DraftBrief> = {}): DraftBrief {
  return {
    piece_type: "article",
    audience: "Local reporters",
    purpose: "Announce the Q3 results",
    tone: "clear and direct",
    target_words: 800,
    transformation_strength: "moderate",
    primary_input_id: null,
    must_include: ["stale-alpha"],
    must_avoid: ["stale-beta"],
    preserve_quotes: true,
    preserve_numbers: true,
    preserve_uncertainty: true,
    drafting_priority: "balanced",
    additional_instructions: "",
    ...overrides,
  };
}

function makeFormValue(brief: DraftBrief): DraftAssignmentFormValue {
  return { title: "Q3 press release", vault_id: 7, mode: "compose", tier: "standard", brief };
}

// Mirrors how DraftWorkspace drives the form: a controlled edit buffer that
// resyncs from the server value whenever the parent supplies a new brief
// (DraftWorkspace.tsx's useEffect on [detail.brief, draft.tier]). The form's
// INTERNAL must_include/must_avoid textarea buffers must follow that resync
// too — that is exactly what AC20 requires.
function ResyncingHarness({
  value,
  onChange,
}: {
  value: DraftAssignmentFormValue;
  onChange: (next: DraftAssignmentFormValue) => void;
}) {
  const [state, setState] = useState(value);
  useEffect(() => {
    setState(value);
  }, [value]);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <DraftAssignmentForm
        value={state}
        onChange={(next) => {
          setState(next);
          onChange(next);
        }}
        variant="edit"
        idPrefix="ac20"
        inputs={[makeInput()]}
      />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mockListAccessibleVaults.mockReset();
  mockListAccessibleVaults.mockResolvedValue({ vaults: [makeVault()] });
});

describe("DraftAssignmentForm (issue #516)", () => {
  // ------------------------------------------------------------------------
  // AC20 (UI-022): when the parent accepts a NEW server brief whose
  // must_include/must_avoid lists changed, BOTH textareas must display the
  // new server list, and the next brief change must emit the new accepted
  // list — not the stale initial text. Current defect: the textareas are
  // init-once useState buffers with no resync on value changes.
  // ------------------------------------------------------------------------
  it("AC20: resyncs must_include/must_avoid textareas and emitted payloads when a new server brief arrives", async () => {
    try {
      const onChange = vi.fn();
      const initial = makeFormValue(
        makeBrief({ must_include: ["stale-alpha"], must_avoid: ["stale-beta"] })
      );
      const { rerender } = render(<ResyncingHarness value={initial} onChange={onChange} />);

      const includeArea0 = await screen.findByLabelText("Must include (one per line)");
      expect(includeArea0).toHaveValue("stale-alpha");
      expect(screen.getByLabelText("Must avoid (one per line)")).toHaveValue("stale-beta");

      // The parent accepts a NEW server brief (e.g. detail refetched after a
      // change elsewhere / mutation elsewhere) with different list contents.
      const next = makeFormValue(
        makeBrief({ must_include: ["fresh-one", "fresh-two"], must_avoid: ["fresh-avoid"] })
      );
      rerender(<ResyncingHarness value={next} onChange={onChange} />);

      // REQUIRED: both textareas display the new server list.
      const includeArea = screen.getByLabelText("Must include (one per line)") as HTMLTextAreaElement;
      const avoidArea = screen.getByLabelText("Must avoid (one per line)") as HTMLTextAreaElement;
      expect(includeArea).toHaveValue("fresh-one\nfresh-two");
      expect(avoidArea).toHaveValue("fresh-avoid");

      // REQUIRED: the next brief change emits the new accepted list, not the
      // stale initial text. The textarea is a plain controlled input (see its
      // onChange), so a single change event exercises the mapping — append
      // one line to whatever the textarea currently shows.
      fireEvent.change(includeArea, { target: { value: `${includeArea.value}\ntyped-extra` } });

      const lastCall = onChange.mock.calls.at(-1)?.[0] as DraftAssignmentFormValue;
      expect(lastCall.brief.must_include).toEqual(["fresh-one", "fresh-two", "typed-extra"]);
      // The untouched list keeps the accepted server value, not the stale one.
      expect(lastCall.brief.must_avoid).toEqual(["fresh-avoid"]);

      console.log("AC20 CHECK: PASS");
    } catch (err) {
      console.log("AC20 CHECK: FAIL");
      throw err;
    }
  });
});
