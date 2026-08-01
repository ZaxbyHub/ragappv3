import { readFileSync } from "fs";
import { resolve } from "path";
import type { ReactNode } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DraftSourceList } from "./DraftSourceList";
import { draftRoomKeys, type DraftInput } from "@/lib/api/draftRoom";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    updateDraftInput: vi.fn(),
    deleteDraftInput: vi.fn(),
  };
});

interface MockSelectProps {
  value?: string;
  onValueChange?: (value: string) => void;
  disabled?: boolean;
  children?: ReactNode;
}
interface MockSelectItemProps {
  value: string;
  children?: ReactNode;
}

vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const SelectCtx = React.createContext<(value: string) => void>(() => {});

  function Select({ onValueChange, children }: MockSelectProps) {
    return React.createElement(
      SelectCtx.Provider,
      { value: onValueChange ?? (() => {}) },
      React.createElement("div", null, children)
    );
  }
  function SelectTrigger({ children }: { children?: ReactNode }) {
    return React.createElement("div", null, children);
  }
  function SelectValue() {
    return null;
  }
  function SelectContent({ children }: { children?: ReactNode }) {
    return React.createElement("div", null, children);
  }
  function SelectItem({ value, children }: MockSelectItemProps) {
    const onValueChange = React.useContext(SelectCtx);
    return React.createElement(
      "button",
      { type: "button", onClick: () => onValueChange(value) },
      children
    );
  }

  return { Select, SelectTrigger, SelectValue, SelectContent, SelectItem };
});

import { updateDraftInput, deleteDraftInput } from "@/lib/api/draftRoom";

const mockUpdateDraftInput = vi.mocked(updateDraftInput);
const mockDeleteDraftInput = vi.mocked(deleteDraftInput);

function makeInput(overrides: Partial<DraftInput> = {}): DraftInput {
  return {
    id: 1,
    role: "reference",
    authority: "unknown",
    as_of_date: null,
    original_name: "brief.pdf",
    extension: ".pdf",
    media_type: "application/pdf",
    size_bytes: 204800,
    content_sha256: "deadbeef",
    parse_status: "ready",
    parse_error: null,
    parsed_char_count: 5000,
    active_parse_job_id: null,
    last_parse_job_id: 3,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderList(
  inputs: DraftInput[],
  props: { locked?: boolean; lockedReason?: string; canEdit?: boolean } = {}
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <DraftSourceList
        draftId={7}
        inputs={inputs}
        locked={props.locked ?? false}
        lockedReason={props.lockedReason}
        canEdit={props.canEdit ?? true}
      />
    </QueryClientProvider>
  );
  return { ...utils, queryClient, invalidateSpy };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("DraftSourceList", () => {
  it("shows the empty state explaining a compile needs at least one ready input", () => {
    renderList([]);
    expect(screen.getByText(/at least one parsed, ready source/i)).toBeInTheDocument();
  });

  it("shows original name, extension, size, role, authority, as-of date and parsed char count", () => {
    renderList([
      makeInput({
        original_name: "quarterly-report.docx",
        extension: ".docx",
        size_bytes: 1048576,
        role: "manuscript",
        authority: "primary",
        as_of_date: "2026-02-01",
        parsed_char_count: 12345,
      }),
    ]);

    expect(screen.getByText("quarterly-report.docx")).toBeInTheDocument();
    expect(screen.getByText(".docx")).toBeInTheDocument();
    expect(screen.getByText("1.0 MB")).toBeInTheDocument();
    expect(screen.getByText("Manuscript")).toBeInTheDocument();
    expect(screen.getByText("Primary")).toBeInTheDocument();
    expect(screen.getByText(/As of/)).toBeInTheDocument();
    expect(screen.getByText("12,345 characters parsed")).toBeInTheDocument();
  });

  it("renders parse_error verbatim for a failed input", () => {
    renderList([
      makeInput({
        parse_status: "failed",
        parse_error: "Could not extract text: password-protected PDF",
      }),
    ]);

    expect(
      screen.getByText("Could not extract text: password-protected PDF")
    ).toBeInTheDocument();
  });

  it("hides edit and delete controls entirely when canEdit is false", () => {
    renderList([makeInput()], { canEdit: false });
    expect(screen.queryByRole("button", { name: /edit/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /remove/i })).not.toBeInTheDocument();
  });

  it("disables editing and shows lockedReason while locked", () => {
    renderList([makeInput()], { locked: true, lockedReason: "Compile in progress" });
    expect(screen.getByText(/Compile in progress/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /edit/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /remove/i })).toBeDisabled();
  });

  it("saves a role/authority/as-of edit via updateDraftInput and invalidates the draft queries", async () => {
    const user = userEvent.setup();
    mockUpdateDraftInput.mockResolvedValue(makeInput({ role: "manuscript", authority: "primary" }));
    const { invalidateSpy } = renderList([makeInput({ id: 5 })]);

    await user.click(screen.getByRole("button", { name: /edit/i }));
    await user.click(screen.getByRole("button", { name: "Manuscript" }));
    await user.click(screen.getByRole("button", { name: "Primary" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockUpdateDraftInput).toHaveBeenCalledTimes(1));
    expect(mockUpdateDraftInput).toHaveBeenCalledWith(7, 5, {
      role: "manuscript",
      authority: "primary",
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.detail(7) });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.inputs(7) });
  });

  it("requires confirmation before deleting, then calls deleteDraftInput", async () => {
    const user = userEvent.setup();
    mockDeleteDraftInput.mockResolvedValue(undefined);
    renderList([makeInput({ id: 9, original_name: "old-notes.txt" })]);

    await user.click(screen.getByRole("button", { name: /remove/i }));
    expect(mockDeleteDraftInput).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog");
    const confirmButton = within(dialog).getByRole("button", { name: "Remove" });
    await user.click(confirmButton);

    await waitFor(() => expect(mockDeleteDraftInput).toHaveBeenCalledWith(7, 9));
  });

  it("does not import useUploadStore or the documents UploadDropzone", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/draft-room/DraftSourceList.tsx"),
      "utf-8"
    );
    expect(source).not.toMatch(/useUploadStore/);
    expect(source).not.toMatch(/documents\/UploadDropzone/);
  });
});
