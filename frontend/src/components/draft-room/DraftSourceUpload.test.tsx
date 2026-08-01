import { readFileSync } from "fs";
import { resolve } from "path";
import type { ComponentProps, ReactNode } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DraftSourceUpload } from "./DraftSourceUpload";
import { ADD_SOURCE_FILES_CTA } from "./labels";
import { draftRoomKeys, type DraftDetail, type DraftInput, type DraftInputUploadResponse } from "@/lib/api/draftRoom";

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({
    getRootProps: () => ({}),
    getInputProps: () => ({}),
    isDragActive: false,
  }),
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    uploadDraftInput: vi.fn(),
    getDraft: vi.fn(),
    // Kept mocked (but never expected to be called) so tests can assert the
    // heavy per-input content endpoint is never used as a status probe.
    getDraftInputContent: vi.fn(),
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
interface MockSelectTriggerProps {
  children?: ReactNode;
  "aria-label"?: string;
  id?: string;
}

vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const SelectCtx = React.createContext<(value: string) => void>(() => {});

  function Select({ onValueChange, disabled, children }: MockSelectProps) {
    return React.createElement(
      SelectCtx.Provider,
      { value: onValueChange ?? (() => {}) },
      React.createElement("div", { "data-disabled": disabled ? "true" : "false" }, children)
    );
  }
  function SelectTrigger({ children, "aria-label": ariaLabel, id }: MockSelectTriggerProps) {
    return React.createElement("div", { role: "group", "aria-label": ariaLabel, id }, children);
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

import { uploadDraftInput, getDraft, getDraftInputContent } from "@/lib/api/draftRoom";

const mockUploadDraftInput = vi.mocked(uploadDraftInput);
const mockGetDraft = vi.mocked(getDraft);
const mockGetDraftInputContent = vi.mocked(getDraftInputContent);

function makeApiError(status: number, code: string, detail: string, context: Record<string, unknown> = {}) {
  return {
    message: detail,
    status,
    name: "APIError",
    originalError: { response: { data: { detail, code, context } } },
  };
}

function renderComponent(
  props: Partial<ComponentProps<typeof DraftSourceUpload>> = {},
  options: { pollIntervalSeconds?: number } = {}
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (options.pollIntervalSeconds != null) {
    // Seeds the capabilities cache the component opportunistically reads
    // from, without this test ever fetching capabilities itself.
    queryClient.setQueryData(draftRoomKeys.capabilities(), {
      limits: { poll_interval_seconds: options.pollIntervalSeconds },
    });
  }
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <DraftSourceUpload draftId={1} maxInputs={10} currentInputCount={0} {...props} />
    </QueryClientProvider>
  );
  return { queryClient, ...utils };
}

function makeDraftInputRecord(overrides: Partial<DraftInput> = {}): DraftInput {
  return {
    id: 42,
    role: "reference",
    authority: "unknown",
    as_of_date: null,
    original_name: "notes.txt",
    extension: ".txt",
    media_type: "text/plain",
    size_bytes: 123,
    content_sha256: "abc",
    parse_status: "pending",
    parse_error: null,
    parsed_char_count: null,
    active_parse_job_id: 7,
    last_parse_job_id: 7,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function makeUploadResponse(overrides: Partial<DraftInput> = {}): DraftInputUploadResponse {
  return {
    input: makeDraftInputRecord(overrides),
    job: {
      id: 7,
      draft_id: 1,
      job_type: "parse_input",
      status: "pending",
      start_stage: null,
      active_stage: null,
      progress_percent: 0,
      model_call_count: 0,
      max_model_calls: 0,
      retry_count: 0,
      parent_job_id: null,
      attempt_no: 1,
      compile_input_sha256: null,
      prompt_bundle_version: null,
      timeout_seconds: 300,
      cancel_requested_at: null,
      heartbeat_at: null,
      error_code: null,
      error_message: null,
      created_at: "2026-01-01T00:00:00Z",
      started_at: null,
      completed_at: null,
    },
  };
}

/** Minimal `DraftDetail` — only `inputs` matters to the component's poll reconciliation. */
function makeDraftDetail(inputs: DraftInput[]): DraftDetail {
  return {
    summary: {
      id: 1,
      vault_id: 1,
      vault_access: "write",
      title: "Test draft",
      mode: "compose",
      status: "draft",
      tier: "standard",
      lock_version: 1,
      current_revision_id: null,
      active_job_id: null,
      input_count: inputs.length,
      open_blocker_count: 0,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      ready_at: null,
    },
    brief: {
      piece_type: "article",
      audience: "general",
      purpose: "inform",
      tone: "neutral",
      target_words: 500,
      transformation_strength: "moderate",
      primary_input_id: null,
      must_include: [],
      must_avoid: [],
      preserve_quotes: false,
      preserve_numbers: false,
      preserve_uncertainty: false,
      drafting_priority: "accuracy",
      additional_instructions: "",
    },
    inputs,
    current_revision_summary: null,
    active_compile_job: null,
    revision_count: 0,
    evidence_count: 0,
    claim_counts_by_status: {},
    finding_counts_by_severity: {},
  };
}

function makeFile(name = "notes.txt") {
  return new File(["hello world"], name, { type: "text/plain" });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetDraft.mockResolvedValue(makeDraftDetail([makeDraftInputRecord({ parse_status: "ready" })]));
});

describe("DraftSourceUpload", () => {
  it("exposes a keyboard-reachable labelled file input", () => {
    renderComponent();
    const input = screen.getByLabelText(ADD_SOURCE_FILES_CTA) as HTMLInputElement;
    expect(input.tagName).toBe("INPUT");
    expect(input.type).toBe("file");
    expect(input).not.toBeDisabled();
  });

  it("uploads with the chosen role, authority and as-of date", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput.mockImplementation(() => new Promise(() => {})); // never resolves for this test
    renderComponent();

    await user.click(screen.getByRole("button", { name: INPUT_ROLE_LABEL("manuscript") }));
    await user.click(screen.getByRole("button", { name: INPUT_AUTHORITY_LABEL("primary") }));

    const dateInput = screen.getByLabelText(/as-of date/i);
    fireEvent.change(dateInput, { target: { value: "2026-01-15" } });

    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA) as HTMLInputElement;
    const file = makeFile();
    await user.upload(fileInput, file);

    await waitFor(() => expect(mockUploadDraftInput).toHaveBeenCalledTimes(1));
    const [draftId, params] = mockUploadDraftInput.mock.calls[0];
    expect(draftId).toBe(1);
    expect(params.file).toBe(file);
    expect(params.role).toBe("manuscript");
    expect(params.authority).toBe("primary");
    expect(params.as_of_date).toBe("2026-01-15");
  });

  it("renders upload progress and transitions queued -> uploading -> parsing -> ready", async () => {
    const user = userEvent.setup();
    let capturedProgress: ((pct: number) => void) | undefined;
    let resolveUpload: (value: DraftInputUploadResponse) => void;
    mockUploadDraftInput.mockImplementation(
      (_draftId, _params, onProgress) =>
        new Promise((resolve) => {
          capturedProgress = onProgress;
          resolveUpload = resolve;
        })
    );
    mockGetDraft
      .mockResolvedValueOnce(makeDraftDetail([makeDraftInputRecord({ parse_status: "pending" })]))
      .mockResolvedValueOnce(makeDraftDetail([makeDraftInputRecord({ parse_status: "ready" })]));

    renderComponent({}, { pollIntervalSeconds: 0.02 });
    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA);
    await user.upload(fileInput, makeFile());

    await waitFor(() => expect(screen.getByText("Uploading")).toBeInTheDocument());
    capturedProgress?.(55);
    await waitFor(() => expect(screen.getByRole("progressbar")).toBeInTheDocument());

    resolveUpload!(makeUploadResponse());
    await waitFor(() => expect(screen.getByText("Parsing")).toBeInTheDocument());

    await waitFor(() => expect(screen.getByText("Parsed")).toBeInTheDocument());
    expect(mockGetDraft).toHaveBeenCalledWith(1);
  });

  it("announces only the terminal ready transition to screen readers, never a progress tick", async () => {
    const user = userEvent.setup();
    let capturedProgress: ((pct: number) => void) | undefined;
    let resolveUpload: (value: DraftInputUploadResponse) => void;
    mockUploadDraftInput.mockImplementation(
      (_draftId, _params, onProgress) =>
        new Promise((resolve) => {
          capturedProgress = onProgress;
          resolveUpload = resolve;
        })
    );
    mockGetDraft
      .mockResolvedValueOnce(makeDraftDetail([makeDraftInputRecord({ parse_status: "pending" })]))
      .mockResolvedValueOnce(makeDraftDetail([makeDraftInputRecord({ parse_status: "ready" })]));

    renderComponent({}, { pollIntervalSeconds: 0.02 });
    const liveRegion = document.querySelector('[aria-live="polite"]') as HTMLElement;
    await user.upload(screen.getByLabelText(ADD_SOURCE_FILES_CTA), makeFile("notes.txt"));

    await waitFor(() => expect(screen.getByText("Uploading")).toBeInTheDocument());
    expect(liveRegion.textContent).toBe("");

    capturedProgress?.(55);
    await waitFor(() => expect(screen.getByRole("progressbar")).toBeInTheDocument());
    expect(liveRegion.textContent).toBe("");

    resolveUpload!(makeUploadResponse());
    await waitFor(() => expect(screen.getByText("Parsing")).toBeInTheDocument());
    expect(liveRegion.textContent).toBe("");

    await waitFor(() => expect(screen.getByText("Parsed")).toBeInTheDocument());
    await waitFor(() => expect(liveRegion.textContent).toBe("notes.txt: parsed."));
  });

  it("announces a terminal parse failure to screen readers", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput.mockResolvedValue(makeUploadResponse());
    mockGetDraft.mockResolvedValue(
      makeDraftDetail([
        makeDraftInputRecord({ parse_status: "failed", parse_error: "Could not extract text: corrupt PDF" }),
      ])
    );

    renderComponent({}, { pollIntervalSeconds: 0.02 });
    const liveRegion = document.querySelector('[aria-live="polite"]') as HTMLElement;
    await user.upload(screen.getByLabelText(ADD_SOURCE_FILES_CTA), makeFile("bad.pdf"));

    await waitFor(() => expect(screen.getByText("Failed")).toBeInTheDocument());
    await waitFor(() => expect(liveRegion.textContent).toBe("bad.pdf: parse failed."));
  });

  it("polls the canonical draft detail to resolve parse state, never the per-input content endpoint", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput.mockResolvedValue(makeUploadResponse());
    mockGetDraft.mockResolvedValue(makeDraftDetail([makeDraftInputRecord({ parse_status: "ready" })]));

    renderComponent({}, { pollIntervalSeconds: 0.02 });
    await user.upload(screen.getByLabelText(ADD_SOURCE_FILES_CTA), makeFile());

    await waitFor(() => expect(screen.getByText("Parsed")).toBeInTheDocument());
    expect(mockGetDraft).toHaveBeenCalledWith(1);
    expect(mockGetDraftInputContent).not.toHaveBeenCalled();
  });

  it("stops polling once every tracked input reaches a terminal parse state", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput.mockResolvedValue(makeUploadResponse());
    mockGetDraft.mockResolvedValue(makeDraftDetail([makeDraftInputRecord({ parse_status: "ready" })]));

    renderComponent({}, { pollIntervalSeconds: 0.02 });
    await user.upload(screen.getByLabelText(ADD_SOURCE_FILES_CTA), makeFile());

    await waitFor(() => expect(screen.getByText("Parsed")).toBeInTheDocument());
    const callsAtTerminal = mockGetDraft.mock.calls.length;
    expect(callsAtTerminal).toBeGreaterThan(0);

    // Wait several poll intervals' worth of real time — no further polling
    // should have happened once the tracked input reached a terminal state.
    await new Promise((r) => setTimeout(r, 100));
    expect(mockGetDraft.mock.calls.length).toBe(callsAtTerminal);
  });

  it("surfaces the server's parse_error verbatim when the detail poll reports a failed parse", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput.mockResolvedValue(makeUploadResponse());
    mockGetDraft.mockResolvedValue(
      makeDraftDetail([
        makeDraftInputRecord({
          parse_status: "failed",
          parse_error: "Could not extract text: corrupt PDF",
        }),
      ])
    );

    renderComponent({}, { pollIntervalSeconds: 0.02 });
    await user.upload(screen.getByLabelText(ADD_SOURCE_FILES_CTA), makeFile());

    await waitFor(() =>
      expect(screen.getByText("Could not extract text: corrupt PDF")).toBeInTheDocument()
    );
    expect(mockGetDraftInputContent).not.toHaveBeenCalled();
  });

  it("shows a distinct message for duplicate_input (409) mentioning the existing input, with no Retry", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput.mockRejectedValue(
      makeApiError(409, "duplicate_input", "input content already exists in this draft", {
        existing_input_id: 99,
      })
    );
    renderComponent();

    await user.upload(screen.getByLabelText(ADD_SOURCE_FILES_CTA), makeFile());

    await waitFor(() =>
      expect(screen.getByText(/input content already exists in this draft/)).toBeInTheDocument()
    );
    expect(screen.getByText(/matches input #99/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /remove/i })).toBeInTheDocument();
  });

  it("shows a distinct message for an oversize (413) upload, with no Retry", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput.mockRejectedValue(
      makeApiError(413, "input_too_large", "file exceeds the maximum allowed size")
    );
    renderComponent();

    await user.upload(screen.getByLabelText(ADD_SOURCE_FILES_CTA), makeFile());

    await waitFor(() =>
      expect(screen.getByText(/file exceeds the maximum allowed size/)).toBeInTheDocument()
    );
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /remove/i })).toBeInTheDocument();
  });

  it("shows a distinct message for an unsupported (415) file type, with no Retry", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput.mockRejectedValue(
      makeApiError(415, "unsupported_input", "file type is not supported")
    );
    renderComponent();

    await user.upload(screen.getByLabelText(ADD_SOURCE_FILES_CTA), makeFile());

    await waitFor(() => expect(screen.getByText(/file type is not supported/)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /remove/i })).toBeInTheDocument();
  });

  it("offers Retry and Remove for a generic/transient failure, and Retry re-uploads", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput
      .mockRejectedValueOnce(makeApiError(500, "internal_error", "something went wrong"))
      .mockResolvedValueOnce(makeUploadResponse());
    renderComponent();

    await user.upload(screen.getByLabelText(ADD_SOURCE_FILES_CTA), makeFile());
    await waitFor(() => expect(screen.getByText(/something went wrong/)).toBeInTheDocument());

    const retryButton = screen.getByRole("button", { name: /retry/i });
    expect(screen.getByRole("button", { name: /remove/i })).toBeInTheDocument();
    await user.click(retryButton);

    await waitFor(() => expect(mockUploadDraftInput).toHaveBeenCalledTimes(2));
  });

  it("removing a failed upload drops the row without calling any API", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput.mockRejectedValue(makeApiError(500, "internal_error", "boom"));
    renderComponent();

    await user.upload(screen.getByLabelText(ADD_SOURCE_FILES_CTA), makeFile());
    await waitFor(() => expect(screen.getByText(/boom/)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /remove/i }));
    expect(screen.queryByText(/boom/)).not.toBeInTheDocument();
  });

  it("shows the cap message and disables upload when currentInputCount >= maxInputs", () => {
    renderComponent({ maxInputs: 3, currentInputCount: 3 });

    expect(screen.getByText(/already holds the maximum of 3 source files/i)).toBeInTheDocument();
    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA) as HTMLInputElement;
    expect(fileInput).toBeDisabled();
  });

  it("shows the disabled reason and disables upload when disabled", () => {
    renderComponent({ disabled: true, disabledReason: "A newsroom run is active." });

    expect(screen.getByText("A newsroom run is active.")).toBeInTheDocument();
    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA) as HTMLInputElement;
    expect(fileInput).toBeDisabled();
  });

  it("does not import useUploadStore or the documents UploadDropzone, and never uses getDraftInputContent for status", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/draft-room/DraftSourceUpload.tsx"),
      "utf-8"
    );
    expect(source).not.toMatch(/useUploadStore/);
    expect(source).not.toMatch(/documents\/UploadDropzone/);
    expect(source).not.toMatch(/getDraftInputContent\(/);
    expect(source).not.toMatch(/import\s*\{[^}]*getDraftInputContent/);
  });
});

function INPUT_ROLE_LABEL(role: "manuscript" | "reference" | "style" | "background" | "challenge") {
  const labels: Record<string, string> = {
    manuscript: "Manuscript",
    reference: "Reference",
    style: "Style sample",
    background: "Background",
    challenge: "Challenge",
  };
  return labels[role];
}

function INPUT_AUTHORITY_LABEL(
  authority: "primary" | "official" | "secondary" | "user_asserted" | "unknown"
) {
  const labels: Record<string, string> = {
    primary: "Primary",
    official: "Official",
    secondary: "Secondary",
    user_asserted: "User asserted",
    unknown: "Unknown",
  };
  return labels[authority];
}
