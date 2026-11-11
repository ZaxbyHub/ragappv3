// frontend/src/lib/api/draftRoom.retryIdempotency.test.ts
//
// Arm's-length acceptance check C4(a) for the draft-capability-liveness
// trace (902-905-911-913). Frozen specification: RED at base, GREEN after
// the fix.
//
// retryDraftJob(draftId, jobId, data, idempotencyKey) must forward the key
// as an `Idempotency-Key` request header, mirroring compileDraft's shape
// (the retry route obeys the same Idempotency-Key contract as compile, so
// the API client must be able to carry it). At base retryDraftJob accepts
// only three parameters and never sends the header.
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockPost } = vi.hoisted(() => ({
  mockPost: vi.fn(),
}));

vi.mock("./core", () => ({
  apiClient: {
    post: mockPost,
  },
  API_BASE_URL: "/api",
}));

import { retryDraftJob } from "./draftRoom";

describe("retryDraftJob — Idempotency-Key header", () => {
  beforeEach(() => {
    mockPost.mockReset().mockResolvedValue({ data: {} });
  });

  it("forwards the supplied idempotency key as the Idempotency-Key header", async () => {
    await retryDraftJob(1, 2, { start_stage: "research" }, "retry-key-1");

    expect(mockPost, "retryDraftJob must forward the key as an Idempotency-Key header").toHaveBeenCalledWith(
      "/draft-room/drafts/1/jobs/2/retry",
      { start_stage: "research" },
      { headers: { "Idempotency-Key": "retry-key-1" } }
    );
  });

  it("still omits the header config when no key is supplied", async () => {
    await retryDraftJob(1, 2, { start_stage: "research" });

    // Two-argument form, matching the pre-existing draftRoom.test.ts pin
    // (keyless calls pass no third argument at all — vitest distinguishes
    // (a, b, undefined) from (a, b), and the repo contract wins).
    expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/1/jobs/2/retry", { start_stage: "research" });
  });
});
