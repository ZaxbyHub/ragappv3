// frontend/src/lib/messageParts.test.ts
// Issue #554 AC6: the typed message-parts model (text / reasoning / source
// part kinds) and its reasoning selector. The reasoning display in
// AssistantMessage reads through this model instead of another ad-hoc
// Message field. The module is loaded dynamically so the pre-fix tree fails
// with the sentinel assertion below instead of a collection-time import
// error.
import { describe, it, expect } from "vitest";

type MessagePartsModule = typeof import("@/lib/messageParts");

async function loadMessageParts(): Promise<MessagePartsModule | null> {
  try {
    // The specifier is assembled at runtime (and marked @vite-ignore) so the
    // pre-fix tree fails INSIDE this try — surfacing the sentinel assertion
    // below — instead of vite failing the whole file at transform time.
    const specifier = ["@/lib", "messageParts"].join("/");
    return await import(/* @vite-ignore */ specifier);
  } catch {
    return null;
  }
}

describe("message parts model (issue #554 AC6)", () => {
  it("defines text/reasoning/source part kinds and selects the reasoning part", async () => {
    const mod = await loadMessageParts();
    expect(
      mod,
      "AC6-C6: @/lib/messageParts must exist and export the typed parts model"
    ).not.toBeNull();
    const { getReasoningPart } = mod as MessagePartsModule;

    const parts = [
      { kind: "text", text: "Answer body." },
      {
        kind: "reasoning",
        text: "Working through the policy section.",
        durationMs: 4200,
        tokensEstimate: 37,
      },
      { kind: "source", source: { id: "s1", filename: "handbook.pdf" } },
    ] as import("@/lib/messageParts").MessagePart[];

    const reasoning = getReasoningPart(parts);
    expect(
      reasoning?.kind,
      "AC6-C6: getReasoningPart must return the reasoning part from a mixed parts array"
    ).toBe("reasoning");
    expect(reasoning?.text).toBe("Working through the policy section.");
    expect(reasoning?.durationMs).toBe(4200);
    expect(reasoning?.tokensEstimate).toBe(37);
  });

  it("returns undefined when no reasoning part exists (text/source parts are distinct kinds)", async () => {
    const mod = await loadMessageParts();
    expect(mod, "AC6-C6: @/lib/messageParts must exist").not.toBeNull();
    const { getReasoningPart } = mod as MessagePartsModule;

    const withoutReasoning = [
      { kind: "text", text: "Answer body." },
      { kind: "source", source: { id: "s1", filename: "handbook.pdf" } },
    ] as import("@/lib/messageParts").MessagePart[];

    expect(
      getReasoningPart(withoutReasoning),
      "AC6-C6: text and source part kinds must not be selected as reasoning"
    ).toBeUndefined();
  });

  it("returns undefined for absent or empty parts", async () => {
    const mod = await loadMessageParts();
    expect(mod, "AC6-C6: @/lib/messageParts must exist").not.toBeNull();
    const { getReasoningPart } = mod as MessagePartsModule;
    expect(getReasoningPart(undefined)).toBeUndefined();
    expect(getReasoningPart(null)).toBeUndefined();
    expect(getReasoningPart([])).toBeUndefined();
  });
});
