/**
 * Issue #1052 regression tests — the Draft Room surface allowlist helper.
 *
 * draftRoomAllowedExtensions must mirror backend DRAFT_ROOM_ALLOWED_EXTENSIONS
 * (backend/app/api/routes/draft_room.py): the server allowlist minus raster
 * images (issue #460 — Draft Room is a parse-only text seam). Entries are
 * normalized to the same lowercase/dot-prefixed form serverAcceptsFilename
 * membership uses, so subtraction never misses an uppercase/dotless variant.
 */
import { describe, expect, it } from "vitest";
import {
  RASTER_IMAGE_EXTENSIONS,
  draftRoomAllowedExtensions,
  normalizeAllowedExtension,
} from "./uploadLimits";

describe("draftRoomAllowedExtensions (issue #1052)", () => {
  it("subtracts every raster image extension from the server allowlist", () => {
    const server = [".pdf", ".docx", ".md", ".txt", ...RASTER_IMAGE_EXTENSIONS];
    expect(draftRoomAllowedExtensions(server)).toEqual([".pdf", ".docx", ".md", ".txt"]);
  });

  it("keeps non-raster extensions untouched and preserves order", () => {
    expect(draftRoomAllowedExtensions([".sql", ".json", ".html"])).toEqual([
      ".sql",
      ".json",
      ".html",
    ]);
  });

  it("normalizes entries before subtracting, so uppercase/dotless variants are still excluded", () => {
    // Backend subtracts set(settings.allowed_extensions) - RASTER_IMAGE_EXTENSIONS
    // on raw entries; the normalized client subtraction covers the sane-config
    // (lowercase dotted) case identically and never leaves ".PNG" behind.
    expect(draftRoomAllowedExtensions(["PNG", ".JPG", "md"])).toEqual([".md"]);
    expect(normalizeAllowedExtension("PNG")).toBe(".png");
  });

  it("returns an empty list for an empty server allowlist", () => {
    expect(draftRoomAllowedExtensions([])).toEqual([]);
  });
});
