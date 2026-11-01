import { describe, expect, it } from "vitest";

import { normalizeCanvasDetail } from "./canvas";

describe("issue #773 Canvas normalization edge cases", () => {
  it("rejects an array in place of the current version", () => {
    expect(() =>
      normalizeCanvasDetail({
        artifact_uid: "cav_malformed_773",
        current_version: [] as never,
      } as never),
    ).toThrow(/missing its version/);
  });
});
