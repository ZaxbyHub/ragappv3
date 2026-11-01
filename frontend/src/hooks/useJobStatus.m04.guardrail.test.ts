// frontend/src/hooks/useJobStatus.m04.guardrail.test.ts
// Issue #784 recurrence guardrail (trace 784-activity-tray-shell-job-center,
// Phase 4.2, defect class C17 "fragmented operability"): every member of
// JOB_FAMILIES must be handled by the Activity tray's aggregation poll.
// Adding a sixth family to the contract vocabulary without registering an
// adapter leaves it silently invisible in the tray — the exact shape of the
// original defect — so this census fails the moment the registry and the
// vocabulary drift apart.

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const sourcePath = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "useJobStatus.ts"
);
const source = readFileSync(sourcePath, "utf8");

const FAMILY_TO_ADAPTER: Record<string, string> = {
  ingest: "fetchIngestRows",
  wiki: "fetchWikiRows",
  "draft-room": "fetchDraftRoomRows",
  kms: "fetchKmsRows",
  reindex: "fetchReindexRows",
};

function jobFamiliesVocabulary(): string[] {
  const match = source.match(/export const JOB_FAMILIES = \[([^\]]*)\]/);
  expect(match, "JOB_FAMILIES vocabulary not found").not.toBeNull();
  return match![1]
    .split(",")
    .map((entry) => entry.trim().replace(/["']/g, ""))
    .filter(Boolean);
}

function aggregationRegistry(): string {
  const match = source.match(
    /async function fetchAllActivityRows\(\)[\s\S]*?\n\}/
  );
  expect(
    match,
    "fetchAllActivityRows aggregation registry not found — the Activity tray has no server-sourced poll loop"
  ).not.toBeNull();
  return match![0];
}

describe("activity tray family census (issue #784, class C17 guardrail)", () => {
  it("maps every JOB_FAMILIES member to a registered adapter", () => {
    const families = jobFamiliesVocabulary();
    for (const family of families) {
      expect(
        FAMILY_TO_ADAPTER[family],
        `JOB_FAMILIES member "${family}" has no known adapter mapping — register it in this census and in fetchAllActivityRows`
      ).toBeDefined();
    }
    expect(Object.keys(FAMILY_TO_ADAPTER).length).toBeGreaterThanOrEqual(
      families.length
    );
  });

  it("registers every family adapter inside the aggregation poll", () => {
    const registry = aggregationRegistry();
    const families = jobFamiliesVocabulary();
    for (const family of families) {
      expect(
        registry,
        `family "${family}" is missing from fetchAllActivityRows — its jobs would be invisible in the tray`
      ).toContain(`${FAMILY_TO_ADAPTER[family]}()`);
    }
  });

  it("defines every referenced adapter as a module-private function", () => {
    for (const adapter of Object.values(FAMILY_TO_ADAPTER)) {
      expect(
        source,
        `adapter ${adapter} is referenced but not defined`
      ).toContain(`async function ${adapter}`);
    }
  });

  it("keeps the census honest: the tray component consumes the aggregation export", () => {
    const traySource = readFileSync(
      path.join(
        path.dirname(fileURLToPath(import.meta.url)),
        "..",
        "components",
        "layout",
        "ActivityTray.tsx"
      ),
      "utf8"
    );
    expect(traySource).toContain("useActivityJobs");
  });
});
