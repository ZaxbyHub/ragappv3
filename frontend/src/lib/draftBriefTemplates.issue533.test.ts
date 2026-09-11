// Regression test for issue #533 (PRR-005): the draftBriefTemplates module
// cache must be resettable so a save in one test cannot leak into the next
// read. The global test setup stubs localStorage as a silent no-op (getItem
// always null, setItem discarded), which is exactly the environment where the
// session cache — not storage — keeps save-then-apply working: after the
// reset, a fresh list must go back to storage and find nothing.
import { describe, expect, it } from "vitest";

import {
  listDraftBriefTemplates,
  resetDraftBriefTemplatesCacheForTests,
  saveDraftBriefTemplate,
} from "@/lib/draftBriefTemplates";
import type { DraftBrief } from "@/lib/api/draftRoom";

const brief: DraftBrief = {
  piece_type: "article",
  audience: "Local reporters",
  purpose: "Announce the Q3 results",
  tone: "clear and direct",
  target_words: 800,
  transformation_strength: "moderate",
  primary_input_id: null,
  must_include: [],
  must_avoid: ["speculation"],
  preserve_quotes: true,
  preserve_numbers: true,
  preserve_uncertainty: true,
  drafting_priority: "balanced",
  additional_instructions: "",
};

describe("draftBriefTemplates (issue #533)", () => {
  it("resetDraftBriefTemplatesCacheForTests clears the session cache so the next list re-reads storage", () => {
    // Baseline: no cached state may survive from an earlier test in this file.
    resetDraftBriefTemplatesCacheForTests();

    const saved = saveDraftBriefTemplate("Quarterly template", brief);
    expect(saved).toHaveLength(1);
    expect(saved[0]).toMatchObject({ name: "Quarterly template", brief });

    // Before the reset the session cache still serves the save — the stubbed
    // storage write alone could not have produced this.
    expect(listDraftBriefTemplates()).toEqual(saved);

    resetDraftBriefTemplatesCacheForTests();

    // After the reset the next list must re-read storage (stubbed empty)
    // rather than serving the stale in-memory copy.
    expect(listDraftBriefTemplates()).toEqual([]);
  });
});
