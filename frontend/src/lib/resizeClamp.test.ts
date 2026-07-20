import { describe, it, expect } from "vitest";
import {
  FILENAME_COL_WIDTH_MIN,
  FILENAME_COL_WIDTH_MAX,
  clampFilenameColWidth,
} from "./resizeClamp";

// WCAG/ARIA range integrity for the filename-column resize separator
// (issue #408 final-critic finding). The keyboard handler in DocumentsPage
// uses this helper to guarantee aria-valuenow stays within [MIN, MAX].
describe("clampFilenameColWidth", () => {
  it("exposes the MIN/MAX constants that match DocumentTable aria-valuemin/max", () => {
    // DocumentTable.tsx hardcodes aria-valuemin={120} aria-valuemax={600}.
    // These constants MUST match — a mismatch would let the keyboard handler
    // drive aria-valuenow outside the declared ARIA range.
    expect(FILENAME_COL_WIDTH_MIN).toBe(120);
    expect(FILENAME_COL_WIDTH_MAX).toBe(600);
  });

  it("passes through values inside the range unchanged", () => {
    expect(clampFilenameColWidth(120)).toBe(120);
    expect(clampFilenameColWidth(250)).toBe(250);
    expect(clampFilenameColWidth(600)).toBe(600);
  });

  it("clamps values above the MAX down to MAX", () => {
    expect(clampFilenameColWidth(601)).toBe(600);
    expect(clampFilenameColWidth(1000)).toBe(600);
    expect(clampFilenameColWidth(Number.MAX_SAFE_INTEGER)).toBe(600);
  });

  it("clamps values below the MIN up to MIN", () => {
    expect(clampFilenameColWidth(119)).toBe(120);
    expect(clampFilenameColWidth(0)).toBe(120);
    expect(clampFilenameColWidth(-100)).toBe(120);
  });

  it("clamps Infinity to MAX and -Infinity to MIN (NaN propagates — not a real input)", () => {
    // The keyboard handler only ever calls this with `width ± 16` where width
    // is a finite useState number, so NaN is not a real input. We assert the
    // documented behavior for the inputs the handler actually produces.
    expect(clampFilenameColWidth(Number.POSITIVE_INFINITY)).toBe(600);
    expect(clampFilenameColWidth(Number.NEGATIVE_INFINITY)).toBe(120);
  });
});
