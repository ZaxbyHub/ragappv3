// Evidence-selection state in useChatShellStore (issue #508 / UI-038): the
// selection must carry the originating message identity end-to-end, and a
// single action must clear every evidence-selection field.
import { describe, it, expect, beforeEach, vi } from "vitest";
import type { Source } from "@/lib/api";

const makeSource = (id: string): Source => ({
  id,
  filename: `${id}.pdf`,
  source_label: "S1",
});

describe("useChatShellStore evidence selection", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("setters store the evidence selection message anchor and focus target", async () => {
    const { useChatShellStore } = await import("./useChatShellStore");
    const state = useChatShellStore.getState();

    state.setSelectedEvidenceSource(makeSource("s1"));
    state.setSelectedEvidenceMessageId("m1");
    state.setEvidenceReturnFocusId("m1");

    const next = useChatShellStore.getState();
    expect(next.selectedEvidenceSource?.id).toBe("s1");
    expect(next.selectedEvidenceMessageId).toBe("m1");
    expect(next.evidenceReturnFocusId).toBe("m1");
  });

  it("resetEvidenceSelection clears source, message anchor, and focus target", async () => {
    const { useChatShellStore } = await import("./useChatShellStore");
    const state = useChatShellStore.getState();

    state.setSelectedEvidenceSource(makeSource("s1"));
    state.setSelectedEvidenceMessageId("m1");
    state.setEvidenceReturnFocusId("m1");

    useChatShellStore.getState().resetEvidenceSelection();

    const cleared = useChatShellStore.getState();
    expect(cleared.selectedEvidenceSource).toBeNull();
    expect(cleared.selectedEvidenceMessageId).toBeNull();
    expect(cleared.evidenceReturnFocusId).toBeNull();
  });
});
