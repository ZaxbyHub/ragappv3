import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Recurrence guardrails for the issue #514 defect classes (Phase 4.2).
 *
 * These read the real source files so a regression of the original defect
 * shape fails here even if no behavioral test covers the touched file:
 *   1. Bare `filter(isUploadTooLarge)` — the callback-arity bug class where
 *      the predicate receives (element, index, array) and the index binds to
 *      the limit parameter (UI-049 / CONFIG-006 companion).
 *   2. The bounded upload transfer pool constant must stay exported and
 *      documented (UPLOAD-DEEP-01).
 *   3. The Progress primitive must forward `value` to the Radix Root so
 *      aria-valuenow exists (UI-051).
 */

const srcRoot = resolve(__dirname, "..");

function readSrc(rel: string): string {
  const p = resolve(srcRoot, rel);
  expect(existsSync(p), `expected source file to exist: ${rel}`).toBe(true);
  return readFileSync(p, "utf-8");
}

function grepAll(pattern: RegExp): string[] {
  const hits: string[] = [];
  const files = [
    "pages/DocumentsPage.tsx",
    "pages/DocumentDetailPage.tsx",
    "pages/KMSDetailPage.tsx",
    "pages/VaultsPage.tsx",
    "components/documents/useDocumentPolling.ts",
    "components/documents/DocumentTable.tsx",
    "components/documents/DocumentCardsList.tsx",
    "components/documents/BulkTagDialog.tsx",
    "components/documents/UploadDropzone.tsx",
    "components/shared/DocumentCard.tsx",
    "components/shared/StatusBadge.tsx",
    "components/shared/UploadIndicator.tsx",
    "components/chat/Composer.tsx",
    "components/ui/progress.tsx",
    "components/ui/radio-group.tsx",
    "components/layout/NavigationRail.tsx",
    "hooks/useUploadMonitoring.ts",
    "stores/useUploadStore.ts",
    "stores/useVaultStore.ts",
    "lib/uploadLimits.ts",
  ];
  for (const rel of files) {
    const p = resolve(srcRoot, rel);
    if (!existsSync(p)) continue;
    const lines = readFileSync(p, "utf-8").split(/\r?\n/);
    lines.forEach((line, i) => {
      if (pattern.test(line)) hits.push(`${rel}:${i + 1}: ${line.trim()}`);
    });
  }
  return hits;
}

describe("issue #514 recurrence guardrails", () => {
  it("GUARD-ARITY: no bare filter(isUploadTooLarge) callback-arity usage remains", () => {
    const hits = grepAll(/filter\(isUploadTooLarge\)/);
    expect(
      hits,
      `bare filter(isUploadTooLarge) reintroduces the (element, index, array) arity bug: ${hits.join("; ")}`
    ).toEqual([]);
  });

  it("GUARD-POOL: the documented bounded transfer pool stays exported and is the only upload caller", () => {
    const store = readSrc("stores/useUploadStore.ts");
    expect(store).toMatch(/UPLOAD_CONCURRENCY\s*=\s*\d+/);
    // The store's pool is the single uploadDocument caller; chat/list surfaces
    // must enqueue through addUploads instead of issuing transfers directly.
    const composer = readSrc("components/chat/Composer.tsx");
    expect(composer).not.toMatch(/uploadDocument\(/);
  });

  it("GUARD-PROGRESS: the Progress primitive forwards value to the Radix Root", () => {
    const progress = readSrc("components/ui/progress.tsx");
    expect(progress).toMatch(/value=\{value\}/);
  });
});
