// frontend/src/components/draft-room/DraftSourceUpload.workerUnavailable.test.tsx
//
// Arm's-length acceptance check C19 (upload leg) for the
// draft-capability-liveness trace (902-905-911-913). Frozen specification:
// RED at base, GREEN after the fix.
//
// When an upload from DraftSourceUpload rejects with a Draft Room error
// whose code is `worker_unavailable` (HTTP 503, body
// {"detail":"draft room worker is unavailable","code":"worker_unavailable"}),
// the Draft Room capabilities query (draftRoomKeys.capabilities()) must be
// invalidated/refetched — the parse worker died and the upload CTA must
// stop being offered for the query's stale window. At base the component's
// error path only explains/toasts.
//
// Harness mirrors DraftSourceUpload.test.tsx (dropzone stub, mocked
// uploadDraftInput) plus a capabilities-query probe in the same
// QueryClient.
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";

import { DraftSourceUpload } from "./DraftSourceUpload";
import { ADD_SOURCE_FILES_CTA } from "./labels";
import type { DraftRoomCapabilities } from "@/lib/api/draftRoom";

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({
    getRootProps: () => ({}),
    getInputProps: () => ({}),
    isDragActive: false,
  }),
}));

const { mockUploadDraftInput, capsQueryFn } = vi.hoisted(() => ({
  mockUploadDraftInput: vi.fn(),
  capsQueryFn: vi.fn(),
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    uploadDraftInput: mockUploadDraftInput,
    getDraft: vi.fn(),
    getDraftInputContent: vi.fn(),
  };
});

import { draftRoomKeys } from "@/lib/api/draftRoom";

function makeCapabilities(): DraftRoomCapabilities {
  return {
    enabled: true,
    modes: ["compose"],
    tiers: ["standard"],
    piece_types: ["article"],
    transformation_strengths: ["light"],
    limits: { max_inputs: 20 },
    export_formats: ["md"],
    logical_model_modes: ["default"],
    default_logical_mode: "default",
    compile_start_stages: ["research"],
    compile_stage_order: ["research", "assemble"],
    prompt_bundle_version: "v1",
    editorial_gates_installed: true,
    compile_available: true,
    findings_available: true,
    claims_available: true,
    evidence_available: true,
    ready_available: true,
    promote_available: true,
    upload_available: true,
  };
}

/** The apiClient-normalized 503 worker_unavailable error (real shape parseDraftRoomError reads). */
function workerUnavailableError(): Error {
  const error = new Error("Request failed with status code 503");
  (error as unknown as { status: number }).status = 503;
  (error as unknown as { originalError: unknown }).originalError = {
    response: {
      status: 503,
      data: {
        detail: "draft room worker is unavailable",
        code: "worker_unavailable",
      },
    },
  };
  return error;
}

/** Mounted consumer of the capabilities query — observes invalidation refetches. */
function CapabilitiesProbe() {
  useQuery({
    queryKey: draftRoomKeys.capabilities(),
    queryFn: capsQueryFn,
    staleTime: Infinity,
    retry: false,
  });
  return null;
}

function renderComponent() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <CapabilitiesProbe />
      <DraftSourceUpload draftId={1} maxInputs={10} currentInputCount={0} />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mockUploadDraftInput.mockReset();
  capsQueryFn.mockReset().mockResolvedValue(makeCapabilities());
});

describe("DraftSourceUpload worker_unavailable invalidation (C19 upload leg)", () => {
  it("refetches the capabilities query when an upload rejects with worker_unavailable", async () => {
    const user = userEvent.setup();
    mockUploadDraftInput.mockRejectedValue(workerUnavailableError());
    renderComponent();

    await waitFor(() => expect(capsQueryFn).toHaveBeenCalledTimes(1));
    // Mount-time baseline, captured BEFORE the action is triggered —
    // the implementation invalidates inside onError, which can land
    // before any later side effect is observed.
    const baseline = capsQueryFn.mock.calls.length;
    const fileInput = screen.getByLabelText(ADD_SOURCE_FILES_CTA) as HTMLInputElement;
    await user.upload(fileInput, new File(["hello draft"], "brief.txt", { type: "text/plain" }));

    await waitFor(() => expect(mockUploadDraftInput).toHaveBeenCalledTimes(1));
    const refetched = await waitFor(
      () => {
        if (capsQueryFn.mock.calls.length > baseline) return true;
        expect(capsQueryFn.mock.calls.length, "capabilities query must refetch").toBeGreaterThan(baseline);
        return false;
      },
      { timeout: 1500 }
    ).then(
      () => true,
      () => false
    );
    expect(
      refetched,
      "a worker_unavailable rejection from upload must invalidate/refetch the Draft Room capabilities query"
    ).toBe(true);
  });
});
