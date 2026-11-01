import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DraftSourceUpload } from "./DraftSourceUpload";
import {
  draftRoomKeys,
  getDraft,
  uploadDraftInput,
  type DraftDetail,
  type DraftInput,
  type DraftInputUploadResponse,
} from "@/lib/api/draftRoom";

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({ getRootProps: () => ({}), isDragActive: false }),
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return { ...actual, getDraft: vi.fn(), uploadDraftInput: vi.fn() };
});

const mockGetDraft = vi.mocked(getDraft);
const mockUploadDraftInput = vi.mocked(uploadDraftInput);
const queryClients: QueryClient[] = [];

function pendingInput(): DraftInput {
  return {
    id: 42, role: "reference", authority: "unknown", as_of_date: null,
    original_name: "notes.txt", extension: ".txt", media_type: "text/plain", size_bytes: 1,
    content_sha256: "hash", parse_status: "pending", parse_error: null, parsed_char_count: null,
    active_parse_job_id: 7, last_parse_job_id: 7, created_at: "2026-01-01T00:00:00Z",
  };
}

function pendingDetail(): DraftDetail {
  return {
    summary: {
      id: 1, vault_id: 1, vault_access: "write", title: "Test", mode: "compose", status: "draft", tier: "standard",
      lock_version: 1, current_revision_id: null, active_job_id: null, input_count: 1, open_blocker_count: 0,
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", ready_at: null,
    },
    brief: {
      piece_type: "article", audience: "general", purpose: "inform", tone: "neutral", target_words: 500,
      transformation_strength: "moderate", primary_input_id: null, must_include: [], must_avoid: [],
      preserve_quotes: false, preserve_numbers: false, preserve_uncertainty: false, drafting_priority: "accuracy",
      additional_instructions: "",
    },
    inputs: [pendingInput()], current_revision_summary: null, active_compile_job: null, revision_count: 0,
    evidence_count: 0, claim_counts_by_status: {}, finding_counts_by_severity: {},
  };
}

function pendingUploadResponse(): DraftInputUploadResponse {
  return {
    input: pendingInput(),
    job: {
      id: 7, draft_id: 1, job_type: "parse_input", status: "pending", start_stage: null, active_stage: null,
      progress_percent: 0, model_call_count: 0, max_model_calls: 0, retry_count: 0, parent_job_id: null,
      attempt_no: 1, compile_input_sha256: null, prompt_bundle_version: null, timeout_seconds: 300,
      cancel_requested_at: null, heartbeat_at: null, error_code: null, error_message: null,
      created_at: "2026-01-01T00:00:00Z", started_at: null, completed_at: null,
    },
  };
}

function renderParsingUpload(pollIntervalSeconds?: number) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClients.push(queryClient);
  if (pollIntervalSeconds !== undefined) {
    queryClient.setQueryData(draftRoomKeys.capabilities(), { limits: { poll_interval_seconds: pollIntervalSeconds } });
  }
  render(
    <QueryClientProvider client={queryClient}>
      <DraftSourceUpload draftId={1} maxInputs={10} currentInputCount={0} />
    </QueryClientProvider>
  );
  fireEvent.change(screen.getByLabelText("Add source files"), {
    target: { files: [new File(["contents"], "notes.txt", { type: "text/plain" })] },
  });
}

async function expectCallsAfter(ms: number, count: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  expect(mockGetDraft).toHaveBeenCalledTimes(count);
}

async function settleInitialQuery() {
  // Flush React's upload effect, TanStack's first query, and its Promise
  // continuation without advancing the fake clock past t=0.
  await act(async () => {
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(0);
    await Promise.resolve();
  });
  expect(mockGetDraft).toHaveBeenCalledTimes(1);
}

afterEach(() => {
  queryClients.forEach((client) => client.clear());
  queryClients.length = 0;
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("issue #773 DraftSourceUpload polling interval", () => {
  it("converts the configured capability interval from seconds to TanStack milliseconds", async () => {
    vi.useFakeTimers();
    mockUploadDraftInput.mockResolvedValue(pendingUploadResponse());
    mockGetDraft.mockResolvedValue(pendingDetail());

    renderParsingUpload(2.5);
    await settleInitialQuery();
    await expectCallsAfter(2499, 1);
    await expectCallsAfter(1, 2);
  });

  it("clamps sub-500ms polling and stops after the server reports parsing complete", async () => {
    vi.useFakeTimers();
    mockUploadDraftInput.mockResolvedValue(pendingUploadResponse());
    mockGetDraft
      .mockResolvedValueOnce(pendingDetail())
      .mockResolvedValue({
        ...pendingDetail(),
        inputs: [{ ...pendingInput(), parse_status: "ready", active_parse_job_id: null }],
      });

    renderParsingUpload(0.1);
    await settleInitialQuery();
    await expectCallsAfter(499, 1);
    await expectCallsAfter(1, 2);
    await vi.waitFor(() => expect(screen.getByText("Parsed")).toBeInTheDocument());
    await expectCallsAfter(1000, 2);
  });

  it.each([undefined, 0, -1])("uses the 2000 ms fallback when poll_interval_seconds is %s", async (configured) => {
    vi.useFakeTimers();
    mockUploadDraftInput.mockResolvedValue(pendingUploadResponse());
    mockGetDraft.mockResolvedValue(pendingDetail());

    renderParsingUpload(configured);
    await settleInitialQuery();
    await expectCallsAfter(1999, 1);
    await expectCallsAfter(1, 2);
  });
});
