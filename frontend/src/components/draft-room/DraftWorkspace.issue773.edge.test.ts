import { describe, expect, it } from "vitest";

import {
  DEFAULT_DRAFT_MAX_INPUTS,
  INCOMPLETE_INPUTS_BLOCKER,
  ZERO_READY_INPUTS_BLOCKER,
  draftCompileInputBlocker,
  draftMaxInputs,
} from "./DraftWorkspace";

describe("issue #773 Draft capability edge contracts", () => {
  it("blocks every mixed parse state while preserving the zero-ready reason", () => {
    expect(draftCompileInputBlocker([])).toBe(ZERO_READY_INPUTS_BLOCKER);
    expect(draftCompileInputBlocker([{ parse_status: "failed" }])).toBe(ZERO_READY_INPUTS_BLOCKER);
    expect(draftCompileInputBlocker([{ parse_status: "ready" }, { parse_status: "pending" }])).toBe(
      INCOMPLETE_INPUTS_BLOCKER,
    );
    expect(draftCompileInputBlocker([{ parse_status: "ready" }, { parse_status: "parsing" }])).toBe(
      INCOMPLETE_INPUTS_BLOCKER,
    );
    expect(draftCompileInputBlocker([{ parse_status: "ready" }, { parse_status: "cancelled" }])).toBe(
      INCOMPLETE_INPUTS_BLOCKER,
    );
    expect(draftCompileInputBlocker([{ parse_status: "ready" }, { parse_status: "ready" }])).toBeNull();
  });

  it("uses the shipped maximum while capabilities are unresolved", () => {
    expect(draftMaxInputs(undefined)).toBe(DEFAULT_DRAFT_MAX_INPUTS);
    expect(draftMaxInputs({ limits: {} } as never)).toBe(DEFAULT_DRAFT_MAX_INPUTS);
    expect(draftMaxInputs({ limits: { max_inputs: 0 } } as never)).toBe(DEFAULT_DRAFT_MAX_INPUTS);
    expect(draftMaxInputs({ limits: { max_inputs: -1 } } as never)).toBe(DEFAULT_DRAFT_MAX_INPUTS);
    expect(draftMaxInputs({ limits: { max_inputs: Number.NaN } } as never)).toBe(DEFAULT_DRAFT_MAX_INPUTS);
    expect(draftMaxInputs({ limits: { max_inputs: Number.POSITIVE_INFINITY } } as never)).toBe(
      DEFAULT_DRAFT_MAX_INPUTS,
    );
    expect(draftMaxInputs({ limits: { max_inputs: 12 } } as never)).toBe(12);
  });
});
