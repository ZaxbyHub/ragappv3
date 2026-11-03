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
  serverAcceptsFilename,
} from "./uploadLimits";

/**
 * Literal copy of backend RASTER_IMAGE_EXTENSIONS
 * (backend/app/services/document_artifacts.py). Deliberately NOT derived from
 * the frontend constant: review PRR-005 found the original guard spread
 * RASTER_IMAGE_EXTENSIONS into its own expected value, so mutating `.webp` to
 * `.svg` left every test green. Editing the backend set must fail this test.
 */
const BACKEND_RASTER_IMAGE_EXTENSIONS = [
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".bmp",
  ".tif",
  ".tiff",
  ".webp",
];

describe("RASTER_IMAGE_EXTENSIONS mirror (issue #1052, review PRR-005)", () => {
  it("stays in sync with the backend raster set it mirrors", () => {
    expect([...RASTER_IMAGE_EXTENSIONS].sort()).toEqual(
      [...BACKEND_RASTER_IMAGE_EXTENSIONS].sort()
    );
  });
});

describe("serverAcceptsFilename missing-suffix mode (review PRR-002)", () => {
  // The two server pipelines differ on extensionless names, so one shared
  // predicate cannot serve both with a single hard-coded rule.
  it("applies the Documents .txt fallback by default (documents.py L2637-2643)", () => {
    expect(serverAcceptsFilename("README", [".txt", ".pdf"])).toBe(true);
    expect(serverAcceptsFilename("README", [".pdf"])).toBe(false);
  });

  it("rejects extensionless names with no fallback, as Draft Room does (draft_input_storage.py L148-153)", () => {
    // With the shipped default allowlist (which includes .txt) the Draft Room
    // server still 415s these, so the client must too.
    for (const name of ["README", "Makefile", "LICENSE", "Dockerfile", ".gitignore"]) {
      expect(serverAcceptsFilename(name, [".md", ".txt", ".pdf"], {
        missingSuffixFallback: null,
      })).toBe(false);
    }
  });

  it("still accepts a genuine extension under the no-fallback mode", () => {
    expect(
      serverAcceptsFilename("notes.md", [".md", ".txt"], { missingSuffixFallback: null })
    ).toBe(true);
  });

  it("never admits a name that sanitizes to empty under the no-fallback mode", () => {
    // secure_filename('') -> '' -> Draft Room renames to 'unnamed_file'
    // (no extension) and rejects; Documents renames to 'unnamed_file.txt'.
    expect(serverAcceptsFilename("???", [".txt"], { missingSuffixFallback: null })).toBe(false);
    expect(serverAcceptsFilename("???", [".txt"])).toBe(true);
  });
});

describe("draftRoomAllowedExtensions (issue #1052)", () => {
  it("subtracts every raster image extension from the server allowlist", () => {
    const server = [".pdf", ".docx", ".md", ".txt", ...BACKEND_RASTER_IMAGE_EXTENSIONS];
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
