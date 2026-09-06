// frontend/src/lib/api/sessions.batch.test.ts
// Issue #507 / PRR-005: the atomic turn batch save and its rolling-restart
// fallback, plus the durable-seq truncate payload (CHAT-006).
//
// sessions.ts imports { apiClient } from "./core"; the "@" alias resolves to
// the same module file, so mocking "@/lib/api/core" replaces the axios
// instance the session endpoints actually use. Importing from
// "@/lib/api/sessions" directly avoids the api barrel's side effects.

import { beforeEach, describe, expect, it, vi } from "vitest";

const postMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/core", () => ({
  apiClient: { post: postMock },
}));

import { addChatMessagesBatch, truncateChatSession } from "@/lib/api/sessions";

const turnPayloads = [
  { role: "user" as const, content: "question", turn_id: "turn-1" },
  { role: "assistant" as const, content: "answer", turn_id: "turn-1" },
];

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

  it("falls back to sequential single-message saves when the batch POST 404s (old backend during a rolling restart)", async () => {
    postMock
      .mockRejectedValueOnce({ response: { status: 404 } })
      .mockResolvedValueOnce({ data: { id: 1, role: "user", content: "question" } })
      .mockResolvedValueOnce({ data: { id: 2, role: "assistant", content: "answer" } });

    const saved = await addChatMessagesBatch(5, turnPayloads);

    expect(saved).toEqual([
      { id: 1, role: "user", content: "question" },
      { id: 2, role: "assistant", content: "answer" },
    ]);
    expect(postMock).toHaveBeenCalledTimes(3);
    // One batch attempt, then one POST per message to the single-message URL.
    expect(postMock.mock.calls[0][0]).toContain("/messages/batch");
    expect(postMock.mock.calls[1][0]).toBe("/chat/sessions/5/messages");
    expect(postMock.mock.calls[2][0]).toBe("/chat/sessions/5/messages");
    expect(postMock.mock.calls[1][1]).toEqual(turnPayloads[0]);
    expect(postMock.mock.calls[2][1]).toEqual(turnPayloads[1]);
  });

  it("rethrows when the batch POST fails with any status other than 404", async () => {
    postMock.mockRejectedValueOnce({ response: { status: 500 } });

    await expect(addChatMessagesBatch(5, turnPayloads)).rejects.toMatchObject({
      response: { status: 500 },
    });
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
});
