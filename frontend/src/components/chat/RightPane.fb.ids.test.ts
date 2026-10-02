// frontend/src/components/chat/RightPane.fb.ids.test.ts
// PR #835 feedback PRR-012d: the a07 extractedIds/tableIds checks assert the
// ABSENCE of duplicate-key warnings, which an implementation could satisfy
// with any collision-free scheme. This pins the actual structural-id
// derivation (message.id + push ordinal) via the module's test seam.

import { describe, it, expect } from "vitest";
import { RightPaneTestInternals } from "./RightPane";
import type { Message } from "@/stores/useChatStore";

const { extractStructuredOutputs } = RightPaneTestInternals;

function assistantMessage(id: string, content: string): Message {
  return { id, role: "assistant", content } as unknown as Message;
}

describe("extracted-output structural ids (fb PRR-012d)", () => {
  it("same-prefix code blocks get message.id + ordinal ids", () => {
    const msg = assistantMessage(
      "t1",
      "```python\nconst alpha = computeAlpha()\n```\n```python\nconst alpha = computeAlphabet()\n```"
    );
    const outputs = extractStructuredOutputs([msg]);
    expect(outputs.map((o) => o.id)).toEqual(["code-t1-0", "code-t1-1"]);
  });

  it("terminated table + trailing unterminated table get distinct ordinals", () => {
    const content = [
      "Two tables follow.",
      "",
      "| a | b |",
      "|---|---|",
      "| 1 | 2 |",
      "",
      "between",
      "",
      "| c | d |",
      "|---|---|",
      "| 3 | 4 |",
    ].join("\n");
    const outputs = extractStructuredOutputs([assistantMessage("t1", content)]);
    expect(outputs.map((o) => o.id)).toEqual(["table-t1-0", "table-t1-1"]);
  });

  it("identical content in two different messages never shares ids", () => {
    const content = "```python\nconst alpha = computeAlpha()\n```";
    const outputs = extractStructuredOutputs([
      assistantMessage("m1", content),
      assistantMessage("m2", content),
    ]);
    // The ordinal is a running counter across the transcript (the contract
    // is collision-freedom, not per-message restart).
    expect(outputs.map((o) => o.id)).toEqual(["code-m1-0", "code-m2-1"]);
    expect(new Set(outputs.map((o) => o.id)).size).toBe(2);
  });
});
