// frontend/e2e/activity-tray.audit1115.geometry.spec.ts — issue-tracer trace
// 1115-1157-1158 (activity API + tray budget), arm's-length acceptance
// checks: collapsed-preference persistence across a reload, and expanded-tray
// geometry against the two surfaces the fixed tray can overlap (the chat
// composer's Send control and the documents table's sticky Actions column).
//
// Written against UI OBSERVABLES ONLY (roles + bounding boxes): the stub
// backend has no /api/activity/jobs route at base (the tray reads the
// per-family stub routes), and after the fix the stub gains the route later —
// seeding goes through the same POST /_e2e/jobs control route either way, so
// the spec never talks to /api/activity/jobs directly.
//
// Helper style copied from activity-tray.m04.spec.ts (login / resetJobs /
// seedJob / trayRows; stub port via E2E_STUB_PORT).

import { test, expect, type Locator, type Page } from "@playwright/test";

const STUB_PORT = process.env.E2E_STUB_PORT || "9090";
const STUB_ORIGIN = `http://localhost:${STUB_PORT}`;

/** Real login form (chat-smoke.spec.ts idiom), landing on /documents. */
async function login(page: Page) {
  await page.goto("/login");
  await page.fill("#login-username", "e2e-user");
  await page.fill("#login-password", "e2e-pass");
  await page.getByRole("button", { name: /sign in/i }).click();
  // Wait for the POST to land before navigating (goto() aborts the in-flight
  // login request and the refresh cookie never gets set).
  await page.waitForURL((url) => !url.pathname.endsWith("/login"), {
    timeout: 20_000,
  });
  await page.goto("/documents");
  await expect(page.locator("nav").first()).toBeVisible({ timeout: 20_000 });
}

/** Reset tray seed state so assertions are independent of earlier tests. */
async function resetJobs(page: Page) {
  const res = await page.request.post(`${STUB_ORIGIN}/_e2e/reset-jobs`, {
    timeout: 10_000,
  });
  expect(res.ok(), "resetting tray seed state failed").toBeTruthy();
}

/** Seed one stub job of `family` (default running); returns its stub id. */
async function seedJob(
  page: Page,
  family: string,
  options: { status?: string; title?: string } = {}
): Promise<number> {
  const res = await page.request.post(`${STUB_ORIGIN}/_e2e/jobs`, {
    data: { family, status: options.status, title: options.title },
    timeout: 10_000,
  });
  expect(res.ok(), `seeding ${family} job failed`).toBeTruthy();
  const body = (await res.json()) as { id: number };
  return body.id;
}

/** The tray's Activity region (poll-populated rows verified by the caller). */
async function trayRegion(page: Page): Promise<Locator> {
  const region = page.getByRole("region", { name: /activity/i });
  await expect(region).toBeVisible({ timeout: 20_000 });
  return region;
}

type Box = { x: number; y: number; width: number; height: number };

/** Strict open-rectangle overlap test (touching edges is NOT an overlap). */
function boxesIntersect(a: Box, b: Box): boolean {
  return (
    a.x < b.x + b.width &&
    a.x + a.width > b.x &&
    a.y < b.y + b.height &&
    a.y + a.height > b.y
  );
}

/** A present bounding box or a named failure (never a silent null pass). */
async function requireBox(locator: Locator, label: string): Promise<Box> {
  const box = await locator.boundingBox();
  expect(box, `${label} must expose a bounding box`).not.toBeNull();
  return box as Box;
}

// The geometry legs run at three desktop viewports: the overlap defect is
// viewport-dependent (reproduces at 1024x768 on /chat and 1280x800/1440x900
// on /documents at base), so each width must carry its own leg.
const VIEWPORTS = [
  { width: 1024, height: 768 },
  { width: 1280, height: 800 },
  { width: 1440, height: 900 },
] as const;

const LONG_TITLE =
  "wiki compile with a deliberately long title for geometry overlap";

/**
 * Seed one running job per family (five rows): the tray renders at its
 * tallest steady state (the row list reaches its scroll cap) AND the ingest
 * seed materializes a documents-table row, which the table-side legs need.
 */
async function seedTrayJobs(page: Page) {
  await seedJob(page, "wiki", { title: LONG_TITLE });
  await seedJob(page, "draft-room");
  await seedJob(page, "kms");
  await seedJob(page, "reindex");
  await seedJob(page, "ingest");
}

test.describe("activity tray persistence + geometry (audit 1115)", () => {
  test("collapsed preference survives reload", async ({ page }) => {
    await login(page);
    await resetJobs(page);
    await seedJob(page, "wiki");

    await page.goto("/documents", { waitUntil: "domcontentloaded" });
    const region = await trayRegion(page);
    await expect(region.getByRole("listitem")).toHaveCount(1, {
      timeout: 20_000,
    });

    // Collapse via the Hide control inside the Activity region.
    await region.getByRole("button", { name: /hide/i }).click();
    await expect(region.getByRole("button", { name: /activity/i })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    await expect(region.getByRole("listitem")).toHaveCount(0);

    // A full reload must remember the collapsed preference. The collapsed
    // state is asserted through RENDER-TIME observables (the header's
    // aria-expanded and the absence of the Hide affordance) because a
    // row-count of 0 alone is indistinguishable from "first poll still in
    // flight" right after a reload. At base the remount re-expands — this is
    // the RED leg.
    await page.reload();
    const afterReload = await trayRegion(page);
    const header = afterReload.getByRole("button", { name: /activity/i });
    await expect(header).toHaveAttribute("aria-expanded", "false");
    await expect(afterReload.getByRole("button", { name: /hide/i })).toHaveCount(0);
    await expect(afterReload.getByRole("listitem")).toHaveCount(0);

    // ...and the header button (aria-expanded) re-expands to the seeded row.
    await header.click();
    await expect(header).toHaveAttribute("aria-expanded", "true");
    await expect(afterReload.getByRole("listitem")).toHaveCount(1, {
      timeout: 20_000,
    });
  });

  test("expanded tray does not intersect the composer Send control", async ({ page }) => {
    await login(page);
    for (const viewport of VIEWPORTS) {
      const tag = `${viewport.width}x${viewport.height}`;
      // Each viewport leg is independent: fresh seeds and a fresh navigation.
      await page.setViewportSize({ ...viewport });
      await resetJobs(page);
      await seedTrayJobs(page);

      await page.goto("/chat", { waitUntil: "domcontentloaded" });
      const send = page.getByRole("button", { name: "Send message" });
      await expect(send).toBeVisible({ timeout: 20_000 });

      const region = await trayRegion(page);
      await expect(region.getByRole("listitem")).toHaveCount(5, {
        timeout: 20_000,
      });

      const trayBox = await requireBox(region, `Activity tray region @${tag}`);
      const sendBox = await requireBox(send, `composer Send control @${tag}`);
      // Measured boxes are logged so the trace can record the base geometry
      // per viewport; soft asserts keep later legs measurable after a fail.
      console.log(
        `[audit1115][chat ${tag}] tray=${JSON.stringify(trayBox)} send=${JSON.stringify(sendBox)}`,
      );
      await expect
        .soft(
          boxesIntersect(trayBox, sendBox),
          `tray ${JSON.stringify(trayBox)} must not overlap Send ${JSON.stringify(sendBox)} @${tag}`,
        )
        .toBe(false);
    }
  });

  test("expanded tray does not intersect the documents table, its last Download control, or the Actions column", async ({ page }) => {
    await login(page);
    for (const viewport of VIEWPORTS) {
      const tag = `${viewport.width}x${viewport.height}`;
      // Each viewport leg is independent: fresh seeds (ingest included, so a
      // documents-table row exists) and a fresh navigation.
      await page.setViewportSize({ ...viewport });
      await resetJobs(page);
      await seedTrayJobs(page);

      await page.goto("/documents", { waitUntil: "domcontentloaded" });
      const table = page.locator("table").first();
      await expect(table).toBeVisible({ timeout: 20_000 });
      const download = page
        .getByRole("button", { name: "Download document" })
        .last();
      await expect(download).toBeVisible({ timeout: 20_000 });
      const actionsHeader = page.getByRole("columnheader", { name: "Actions" });
      await expect(actionsHeader).toBeVisible({ timeout: 20_000 });

      const region = await trayRegion(page);
      await expect(region.getByRole("listitem")).toHaveCount(5, {
        timeout: 20_000,
      });

      const trayBox = await requireBox(region, `Activity tray region @${tag}`);
      const tableBox = await requireBox(table, `documents table @${tag}`);
      const downloadBox = await requireBox(
        download,
        `last Download document button @${tag}`,
      );
      const actionsBox = await requireBox(
        actionsHeader,
        `documents Actions columnheader @${tag}`,
      );
      console.log(
        `[audit1115][documents ${tag}] tray=${JSON.stringify(trayBox)} table=${JSON.stringify(tableBox)} download=${JSON.stringify(downloadBox)} actions=${JSON.stringify(actionsBox)}`,
      );
      // Soft asserts: every leg's overlap verdict is reported even when an
      // earlier viewport already failed (the test still ends RED).
      await expect
        .soft(
          boxesIntersect(trayBox, tableBox),
          `tray ${JSON.stringify(trayBox)} must not overlap table ${JSON.stringify(tableBox)} @${tag}`,
        )
        .toBe(false);
      await expect
        .soft(
          boxesIntersect(trayBox, downloadBox),
          `tray ${JSON.stringify(trayBox)} must not overlap Download control ${JSON.stringify(downloadBox)} @${tag}`,
        )
        .toBe(false);
      await expect
        .soft(
          boxesIntersect(trayBox, actionsBox),
          `tray ${JSON.stringify(trayBox)} must not overlap Actions ${JSON.stringify(actionsBox)} @${tag}`,
        )
        .toBe(false);
    }
  });
});
