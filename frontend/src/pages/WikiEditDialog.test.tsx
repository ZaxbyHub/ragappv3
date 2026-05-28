import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import type { WikiPage } from "@/lib/api";

// ---------------------------------------------------------------------------
// Radix Select cannot be opened in jsdom (gotcha #2). WikiEditDialog is the
// sole ui/select consumer in this isolated file, so mock the primitive with a
// context (approach a) and drive onValueChange by clicking the SelectItem stub.
// Each <Select> wraps its own items in its own provider, so a single context
// correctly routes a clicked item to its own Select's onValueChange.
// Factory is hoisted, so import React dynamically inside it (gotcha #4).
// ---------------------------------------------------------------------------
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const Ctx = React.createContext<(v: string) => void>(() => {});
  return {
    Select: ({ onValueChange, children }: any) =>
      React.createElement(Ctx.Provider, { value: onValueChange }, children),
    SelectTrigger: ({ children, id }: any) =>
      React.createElement("div", { role: "group", id }, children),
    SelectValue: ({ placeholder }: any) =>
      React.createElement("span", null, placeholder),
    SelectContent: ({ children }: any) => React.createElement("div", null, children),
    SelectItem: ({ value, children }: any) => {
      const onValueChange = React.useContext(Ctx);
      return React.createElement(
        "button",
        { type: "button", "data-select-item": value, onClick: () => onValueChange(value) },
        children,
      );
    },
  };
});

// ---------------------------------------------------------------------------
import { WikiEditDialog } from "./WikiEditDialog";

// rAF is used by the toolbar for caret restore. Run it synchronously so the
// scheduled callback fires deterministically; we assert on the textarea VALUE
// (onChange result), never caret position, to avoid flakiness.
beforeEach(() => {
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    cb(0);
    return 0;
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

const markdownField = () =>
  screen.getByLabelText("Content (Markdown)") as HTMLTextAreaElement;
const titleField = () => screen.getByLabelText("Title") as HTMLInputElement;

function renderDialog(props: Partial<React.ComponentProps<typeof WikiEditDialog>> = {}) {
  const onSave = props.onSave ?? vi.fn().mockResolvedValue(undefined);
  const onClose = props.onClose ?? vi.fn();
  const utils = render(
    <WikiEditDialog
      open={props.open ?? true}
      page={props.page}
      vaultId={props.vaultId ?? 1}
      onClose={onClose}
      onSave={onSave}
    />,
  );
  return { ...utils, onSave, onClose };
}

describe("WikiEditDialog", () => {
  // 1. Render gating
  it("renders title and markdown fields when open", () => {
    renderDialog({ open: true });
    expect(titleField()).toBeInTheDocument();
    expect(markdownField()).toBeInTheDocument();
    expect(screen.getByText("New Wiki Page")).toBeInTheDocument();
  });

  it("does not render dialog content when open={false}", () => {
    renderDialog({ open: false });
    expect(screen.queryByLabelText("Title")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Content (Markdown)")).not.toBeInTheDocument();
  });

  // 2. Toolbar insertion
  it("inserts bold markers around the selection when Bold is clicked", () => {
    renderDialog({ open: true });
    const ta = markdownField();
    // Replace content with a known string and select the word "world".
    fireEvent.change(ta, { target: { value: "hello world" } });
    ta.selectionStart = 6;
    ta.selectionEnd = 11;
    fireEvent.click(screen.getByTitle("Bold"));
    expect(ta.value).toBe("hello **world**");
  });

  it("inserts the Code placeholder when nothing is selected", () => {
    renderDialog({ open: true });
    const ta = markdownField();
    fireEvent.change(ta, { target: { value: "" } });
    ta.selectionStart = 0;
    ta.selectionEnd = 0;
    fireEvent.click(screen.getByTitle("Code"));
    expect(ta.value).toBe("`code`");
  });

  // 3. Save validation + payload
  it("blocks save and shows an error when the title is empty", () => {
    const { onSave } = renderDialog({ open: true });
    // Default create-mode title is empty; click Save.
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("Title is required")).toBeInTheDocument();
  });

  it("calls onSave once with the entered data when title is valid", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { onClose } = renderDialog({ open: true, onSave });

    fireEvent.change(titleField(), { target: { value: "My Entity" } });
    fireEvent.change(screen.getByLabelText("Summary"), {
      target: { value: "A summary" },
    });
    fireEvent.change(markdownField(), { target: { value: "# Body" } });
    // Drive the status Select to "verified" via the mocked SelectItem.
    fireEvent.click(screen.getByRole("button", { name: "verified" }));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith({
      title: "My Entity",
      page_type: "entity",
      slug: undefined,
      markdown: "# Body",
      summary: "A summary",
      status: "verified",
      confidence: 0,
    });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  // 4. onSave rejection
  it("surfaces an error and resets saving state when onSave rejects", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("Server exploded"));
    const { onClose } = renderDialog({ open: true, onSave });

    fireEvent.change(titleField(), { target: { value: "Valid Title" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    await waitFor(() =>
      expect(screen.getByText("Server exploded")).toBeInTheDocument(),
    );
    expect(onClose).not.toHaveBeenCalled();
    // saving state reset → button is re-enabled and back to "Save".
    const saveBtn = screen.getByRole("button", { name: "Save" });
    expect(saveBtn).not.toBeDisabled();
  });

  // 5. Template auto-fill
  it("fills the markdown with the template for a newly chosen page type (create mode)", () => {
    renderDialog({ open: true });
    // Default markdown is the entity template; switch to "procedure".
    fireEvent.click(screen.getByRole("button", { name: "procedure" }));
    const ta = markdownField();
    expect(ta.value).toContain("## Purpose");
    expect(ta.value).toContain("## Steps");
  });

  it("does NOT overwrite user-edited markdown when the page type changes", () => {
    renderDialog({ open: true });
    const ta = markdownField();
    // User types custom content that is not a template.
    fireEvent.change(ta, { target: { value: "My own notes, untouched." } });
    fireEvent.click(screen.getByRole("button", { name: "system" }));
    expect(ta.value).toBe("My own notes, untouched.");
  });

  it("does not auto-fill template in edit mode (existing page)", () => {
    const page: WikiPage = {
      id: 5,
      vault_id: 1,
      slug: "existing",
      title: "Existing Page",
      page_type: "entity",
      markdown: "Original content body.",
      summary: "sum",
      status: "draft",
      confidence: 0.5,
      created_by: null,
      created_at: "",
      updated_at: "",
      last_compiled_at: null,
      claims: [],
      entities: [],
      lint_findings: [],
    };
    renderDialog({ open: true, page });
    expect(screen.getByText("Edit Page")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "procedure" }));
    expect(markdownField().value).toBe("Original content body.");
  });
});
