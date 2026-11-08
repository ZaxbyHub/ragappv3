// frontend/src/lib/api/sessions.batch.test.ts
// Issue #507 / PRR-005: the atomic turn batch save and its rolling-restart
// fallback, plus the durable-seq truncate payload (CHAT-006).
//
// Issue #1078: the 404-fallback legs now drive the error shape the api
// client ACTUALLY rejects with. core.ts's response interceptor returns a
// normalized Error whose status is at the TOP level and which has NO
// `.response` property (`originalError` nests the raw axios error), so the
// pre-#1078 legs — which mocked the raw axios shape `{ response: { status:
// 404 } }` callers never receive — certified a fallback that was dead in
// production. The raw shape stays as a control leg (AC2: the widening is a
// superset; raw-shape behavior must not regress), plus the re-wrapped tier
// where the status only survives under `originalError.response.status`.
//
// sessions.ts imports { apiClient, httpStatusOf } from "./core"; the "@"
// alias resolves to the same module file, so mocking "@/lib/api/core"
// replaces the axios instance the session endpoints actually use. The mock
// spreads the REAL core exports (importOriginal) so the reader under test
// runs the production httpStatusOf — only the axios instance is faked.
// Importing from "@/lib/api/sessions" directly avoids the api barrel's side
// effects.

import { beforeEach, describe, expect, it, vi } from "vitest";

const postMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./core")>()),
  apiClient: { post: postMock },
}));

import { addChatMessagesBatch, truncateChatSession } from "@/lib/api/sessions";

const turnPayloads = [
  { role: "user" as const, content: "question", turn_id: "turn-1" },
  { role: "assistant" as const, content: "answer", turn_id: "turn-1" },
];

/**
 * Replica of core.ts's interceptor normalizer tail (the shape every
 * apiClient rejection carries): top-level `status`, raw axios error under
 * `originalError`, and NO `response` property on the error itself.
 */
function normalizedReject(status: number): Error {
  const error = new Error(`Request failed with status code ${status}`);
  error.name = "APIError";
  (error as unknown as { status?: number }).status = status;
  (error as unknown as { originalError?: unknown }).originalError = {
    response: { status },
  };
  return error;
}

/**
 * The re-wrapped tier: a normalized error whose top-level `status` is
 * missing, so the status only survives under
 * `originalError.response.status` (the shape a consumer that re-throws an
 * enriched copy — e.g. canvas.ts's save-error enrichment — produces).
 */
function originalTierReject(status: number): Error {
  const error = new Error(`Request failed with status code ${status}`);
  error.name = "APIError";
  (error as unknown as { originalError?: unknown }).originalError = {
    response: { status },
  };
  return error;
}

describe("addChatMessagesBatch (issue #507 / PRR-005)", () => {
  beforeEach(() => {
    postMock.mockReset();
  });

  it("saves the whole turn via ONE POST to the batch endpoint", async () => {
    postMock.mockResolvedValueOnce({ data: { messages: [{ id: 1 }, { id: 2 }] } });

    const saved = await addChatMessagesBatch(5, turnPayloads);

    expect(saved).toEqual([{ id: 1 }, { id: 2 }]);
    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock.mock.calls[0][0]).toContain("/chat/sessions/5/messages/batch");
    expect(postMock.mock.calls[0][1]).toEqual({ messages: turnPayloads });
  });

  it("falls back to sequential saves when the batch POST 404s with the api client's NORMALIZED error (top-level status)", async () => {
    // Rows carry the server-issued seq per the single-message endpoint's
    // response contract (issue #683) — the fallback feeds the same migrateId.
    postMock
      .mockRejectedValueOnce(normalizedReject(404))
      .mockResolvedValueOnce({ data: { id: 1, role: "user", content: "question", seq: 1 } })
      .mockResolvedValueOnce({ data: { id: 2, role: "assistant", content: "answer", seq: 2 } });

    const saved = await addChatMessagesBatch(5, turnPayloads);

    expect(saved).toEqual([
      { id: 1, role: "user", content: "question", seq: 1 },
      { id: 2, role: "assistant", content: "answer", seq: 2 },
    ]);
    expect(postMock).toHaveBeenCalledTimes(3);
    // One batch attempt, then one POST per message to the single-message URL.
    expect(postMock.mock.calls[0][0]).toContain("/messages/batch");
    expect(postMock.mock.calls[1][0]).toBe("/chat/sessions/5/messages");
    expect(postMock.mock.calls[2][0]).toBe("/chat/sessions/5/messages");
    expect(postMock.mock.calls[1][1]).toEqual(turnPayloads[0]);
    expect(postMock.mock.calls[2][1]).toEqual(turnPayloads[1]);
  });

  it("falls back to sequential saves when the normalized 404 carries its status only under originalError.response.status (re-wrapped tier)", async () => {
    postMock
      .mockRejectedValueOnce(originalTierReject(404))
      .mockResolvedValueOnce({ data: { id: 1, role: "user", content: "question", seq: 1 } })
      .mockResolvedValueOnce({ data: { id: 2, role: "assistant", content: "answer", seq: 2 } });

    const saved = await addChatMessagesBatch(5, turnPayloads);

    expect(saved).toEqual([
      { id: 1, role: "user", content: "question", seq: 1 },
      { id: 2, role: "assistant", content: "answer", seq: 2 },
    ]);
    expect(postMock).toHaveBeenCalledTimes(3);
  });

  it("still falls back for the RAW axios shape { response: { status: 404 } } (control leg — the widening is a superset)", async () => {
    postMock
      .mockRejectedValueOnce({ response: { status: 404 } })
      .mockResolvedValueOnce({ data: { id: 1, role: "user", content: "question", seq: 1 } })
      .mockResolvedValueOnce({ data: { id: 2, role: "assistant", content: "answer", seq: 2 } });

    const saved = await addChatMessagesBatch(5, turnPayloads);

    expect(saved).toEqual([
      { id: 1, role: "user", content: "question", seq: 1 },
      { id: 2, role: "assistant", content: "answer", seq: 2 },
    ]);
    expect(postMock).toHaveBeenCalledTimes(3);
  });

  it("rethrows when the batch POST fails with a normalized error of any status other than 404", async () => {
    const rejection = normalizedReject(500);
    postMock.mockRejectedValueOnce(rejection);

    await expect(addChatMessagesBatch(5, turnPayloads)).rejects.toBe(rejection);
    expect(postMock).toHaveBeenCalledTimes(1);
  });
});

describe("truncateChatSession (issue #507 / CHAT-006, PRR-020)", () => {
  beforeEach(() => {
    postMock.mockReset();
  });

  it("POSTs the durable seq boundary as keep_seq (not keep_count)", async () => {
    postMock.mockResolvedValueOnce({ data: { remaining_count: 2, tail_seq: 2 } });

    const result = await truncateChatSession(5, 2);

    expect(result).toEqual({ remaining_count: 2, tail_seq: 2 });
    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock.mock.calls[0][0]).toBe("/chat/sessions/5/truncate");
    expect(postMock.mock.calls[0][1]).toEqual({ keep_seq: 2 });
  });

  it("POSTs the observed tail precondition: expected_tail_seq AND the ABA-proof expected_tail_id (issue #684 review F2)", async () => {
    // Both fields are OPTIONAL body members, so tsc/eslint stay silent if a
    // refactor drops either; this wire pin is the one place a silent drop
    // fails the suite. The id is the ABA-proof half (per-session seqs are
    // reused after a truncate+resave; the AUTOINCREMENT PK is not).
    postMock.mockResolvedValueOnce({ data: { remaining_count: 2, tail_seq: 2 } });

    await truncateChatSession(5, 2, { seq: 4, id: 4 });

    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock.mock.calls[0][1]).toEqual({
      keep_seq: 2,
      expected_tail_seq: 4,
      expected_tail_id: 4,
    });
  });
});
