// frontend/src/hooks/useJobStatus.m03.test.ts
// Issue-trace 783-kms-jobhandle-ingest-cancel — acceptance check C6 (AC2).
//
// The shared job-family contract: frontend/src/hooks/useJobStatus.ts must
// export a JOB_FAMILIES constant enumerating exactly "ingest", "wiki",
// "draft-room", "kms", "reindex". At base the module does not exist, so the
// import itself fails (vitest reports "Failed to resolve import ...") — that
// is the intended pre-fix RED signature, not a weakened one.

import { describe, it, expect } from "vitest";

import { JOB_FAMILIES } from "@/hooks/useJobStatus";

describe("useJobStatus m03 (issue-trace 783-kms-jobhandle-ingest-cancel)", () => {
  it("names all five job families", () => {
    expect([...JOB_FAMILIES].sort()).toEqual([
      "draft-room",
      "ingest",
      "kms",
      "reindex",
      "wiki",
    ]);
  });
});
