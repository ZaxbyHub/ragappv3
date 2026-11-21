// frontend/src/hooks/useJobStatus.m04.guardrail.test.ts
// Issue #784 recurrence guardrail (trace 784-activity-tray-shell-job-center,
// Phase 4.2, defect class C17 "fragmented operability"), re-pointed for the
// #1157 aggregate seam. The census originally keyed the five per-family
// adapters (fetchIngestRows & co.) inside fetchAllActivityRows; those
// adapters — and the per-family API clients, the vault-store consultation,
// and the auth-lifecycle role gate they fed — are DELETED BY DESIGN, so the
// structural guarantees now take their #1157 shapes:
//   1. every JOB_FAMILIES member must still have a mapping branch in
//      mapActivityJob — adding a sixth family to the vocabulary without a
//      case branch leaves it silently invisible in the tray (the exact
//      shape of the original defect);
//   2. the aggregation poll must issue exactly ONE request — the aggregate
//      client getActivityJobs — and none of the deleted per-family fetchers;
//   3. import hygiene: the module must not import (or call) any of the
//      deleted per-family list functions, the vault store, or the
//      auth-lifecycle capture helper, and must import getActivityJobs;
//   4. the tray component still consumes the aggregation export.
// Same scanning idiom as before: read the module source and pin its
// structure, so the census fails the moment the registry and the vocabulary
// drift apart.

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const sourcePath = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "useJobStatus.ts"
);
const source = readFileSync(sourcePath, "utf8");

/**
 * Identifiers the #1157 refactor deleted from this module. Their absence
 * must hold for imports AND calls (the whole-source scan), so a reintroduced
 * per-family fan-out — the waste the aggregate endpoint replaced — fails the
 * census wherever it reappears.
 */
const DELETED_IDENTIFIERS = [
  "listDocuments",
  "listReindexJobs",
  "listWikiJobs",
  "listKMSJobs",
  "listDrafts",
  "listDraftJobs",
  "useVaultStore",
  "publishAuthPrincipal",
  "httpStatusOf",
] as const;

function jobFamiliesVocabulary(): string[] {
  const match = source.match(/export const JOB_FAMILIES = \[([^\]]*)\]/);
  expect(match, "JOB_FAMILIES vocabulary not found").not.toBeNull();
  return match![1]
    .split(",")
    .map((entry) => entry.trim().replace(/["']/g, ""))
    .filter(Boolean);
}

function mapActivityJobRegistry(): string {
  const match = source.match(
    /function mapActivityJob\(job: ActivityJob\) \{[\s\S]*?\n\}/
  );
  expect(
    match,
    "mapActivityJob mapping registry not found — the Activity tray has no family-discriminated row mapping"
  ).not.toBeNull();
  return match![0];
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

describe("activity tray aggregate census (issues #784 / #1157, class C17 guardrail)", () => {
  it("maps every JOB_FAMILIES member to a branch in mapActivityJob", () => {
    const registry = mapActivityJobRegistry();
    const families = jobFamiliesVocabulary();
    expect(families.length).toBeGreaterThanOrEqual(5);
    for (const family of families) {
      expect(
        registry,
        `family "${family}" is missing from mapActivityJob — its jobs would be invisible in the tray`
      ).toContain(`case "${family}":`);
    }
  });

  it("issues exactly one request inside the aggregation poll — the aggregate client, none of the deleted fetchers", () => {
    const registry = aggregationRegistry();
    // One tick, ONE aggregate call (#1157): the registry dispatches
    // getActivityJobs exactly once and references no per-family fetcher.
    expect(registry.match(/getActivityJobs\(\)/g)).toHaveLength(1);
    for (const identifier of DELETED_IDENTIFIERS) {
      expect(
        registry,
        `fetchAllActivityRows references deleted identifier "${identifier}" — the aggregate endpoint is the poller's only request`
      ).not.toContain(identifier);
    }
  });

  it("keeps the deleted per-family clients deleted: the module imports getActivityJobs and nothing from the old fan-out", () => {
    expect(source).toContain('import { getActivityJobs } from "@/lib/api";');
    for (const identifier of DELETED_IDENTIFIERS) {
      expect(
        source,
        `module references deleted identifier "${identifier}" — the per-family fan-out, vault-store consultation, and auth-lifecycle role gate must stay deleted (#1157)`
      ).not.toContain(identifier);
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
