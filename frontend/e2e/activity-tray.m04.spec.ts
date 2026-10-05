// frontend/e2e/activity-tray.m04.spec.ts — issue-tracer trace
// 784-activity-tray-shell-job-center, acceptance check C7 (AC2/AC4).
//
// Two e2e tests against the REAL built app (vite preview :4173) with the
// stub backend (:9090). Selectors are resilient (aria roles only — no
// brittle CSS), matching the chat-smoke.spec.ts convention.
//
// ----------------------------------------------------------------------------
// STUB ROUTE REQUIREMENTS beyond what stub-backend.mjs already implements.
// The stub today covers: auth (setup-status/csrf/login/refresh/me/logout),
// health, llm-health modes, vaults, documents list/stats/upload (issue
// #781), chat sessions CRUD + batch + SSE stream, and the onboarding
// milestone routes (issue #782). The Activity tray ADDITIONALLY needs,
// from the implementer (all being ADDED to stub-backend.mjs by the fix —
// this spec is written against them):
//
//   READ routes the tray's useActivityJobs aggregation polls:
//     * GET  /api/documents                    — EXISTS at base (ingest
//       family rows derive from the document list's processing state).
//     * GET  /api/wiki/jobs                   — wiki family jobs.
//     * GET  /api/kms/jobs                    — kms family jobs.
//     * GET  /api/drafts                      — the draft list (to know
//       which drafts to poll jobs for).
//     * GET  /api/drafts/{id}/jobs            — draft-room family jobs.
//     * GET  /api/documents/reindex/jobs      — reindex family jobs.
//
//   ACTION routes the tray's Cancel/Retry buttons POST:
//     * POST /api/wiki/jobs/{id}/cancel       — cancelWikiJob
//     * POST /api/drafts/{id}/jobs/{id}/cancel — cancelDraftJob
//     * POST /api/drafts/{id}/jobs/{id}/retry — retryDraftJob
//     * POST /api/documents/{id}/cancel       — cancelDocumentIngest
//       (new client, core.ts, POST /documents/{file_id}/cancel)
//
//   E2E SEEDING CONTROL routes (test-only, like the /_e2e/* routes the
//   #782 specs use):
//     * POST /_e2e/jobs                       — body { family, status?,
//       title? } seeds one job of that family (default status "running")
//       into the stub's job state so the tray has something to list.
//     * GET  /_e2e/jobs?family={family}       — returns { jobs: [...] }
//       with each job's status, so assertions read stub state directly
//       via page.request instead of UI text.
//
// Spec-side stub port: direct stub API calls use
// process.env.E2E_STUB_PORT || "9090" (first-run-baseline.m01.spec.ts
// idiom). Playwright baseURL stays :4173 and page.request(...) calls ride
// the vite preview proxy.
// ----------------------------------------------------------------------------

import { test, expect, type Page } from "@playwright/test";

const STUB_PORT = process.env.E2E_STUB_PORT || "9090";
const STUB_ORIGIN = `http://localhost:${STUB_PORT}`;

const FAMILIES = ["ingest", "wiki", "draft-room", "kms", "reindex"] as const;

/** Real login form (chat-smoke.spec.ts idiom), landing on /documents. */
async function login(page: Page) {
  await page.goto("/login");
  await page.fill("#login-username", "e2e-user");
  await page.fill("#login-password", "e2e-pass");
  await page.getByRole("button", { name: /sign in/i }).click();
  // "/" redirects to /documents — go there explicitly.
  await page.goto("/documents");
  await expect(page.locator("nav").first()).toBeVisible({ timeout: 20_000 });
}

/** Seed one stub job of `family` (default running) with a stable title. */
async function seedJob(
  page: Page,
  family: string,
  options: { status?: string; title?: string } = {}
) {
  const res = await page.request.post(`${STUB_ORIGIN}/_e2e/jobs`, {
    data: { family, status: options.status, title: options.title },
    timeout: 10_000,
  });
  expect(res.ok(), `seeding ${family} job failed`).toBeTruthy();
}

/** The stub's current job statuses for one family (direct request). */
async function stubJobStatuses(page: Page, family: string): Promise<string[]> {
  const res = await page.request.get(
    `${STUB_ORIGIN}/_e2e/jobs?family=${encodeURIComponent(family)}`,
    { timeout: 10_000 }
  );
  if (!res.ok()) return [];
  const { jobs } = (await res.json()) as { jobs?: Array<{ status?: string }> };
  return (jobs ?? []).map((job) => String(job.status ?? ""));
}

/** Expand the tray if it renders collapsed, then return its row locator.
 *  Resilient: when the region exposes rows directly, no click is needed. */
async function expandTray(page: Page) {
  const region = page.getByRole("region", { name: /activity/i });
  await expect(region).toBeVisible({ timeout: 20_000 });
  const rows = region.getByRole("listitem");
  if (!(await rows.first().isVisible().catch(() => false))) {
    const toggle = page.getByRole("button", { name: /activity/i }).first();
    await toggle.click({ timeout: 20_000 });
  }
  return { region, rows };
}

test.describe("activity tray (trace 784-activity-tray-shell-job-center / C7)", () => {
  test("lists all five families after a reload", async ({ page }) => {
    await login(page);

    // Seed one running job per family through the stub's control route.
    for (const family of FAMILIES) {
      await seedJob(page, family);
    }

    await page.goto("/documents");

    // The Activity landmark is a shell surface: visible on /documents.
    const { region, rows } = await expandTray(page);

    await expect(rows).toHaveCount(5, { timeout: 20_000 });
    for (const family of FAMILIES) {
      await expect(
        region.getByText(new RegExp(family, "i")).first()
      ).toBeVisible({ timeout: 20_000 });
    }

    // Reload-survival: the tray lists the server's jobs again after a full
    // page load — the rows are not client-session-only state.
    await page.reload();
    const afterReload = await expandTray(page);
    await expect(afterReload.rows).toHaveCount(5, { timeout: 20_000 });
    for (const family of FAMILIES) {
      await expect(
        afterReload.region.getByText(new RegExp(family, "i")).first()
      ).toBeVisible({ timeout: 20_000 });
    }
  });

  test("cancel and retry reach the stub server", async ({ page }) => {
    await login(page);

    // Self-contained seeds: one running job per cancellable family plus one
    // FAILED wiki job (distinctive title) for the retry leg.
    await seedJob(page, "wiki");
    await seedJob(page, "draft-room");
    await seedJob(page, "ingest");
    await seedJob(page, "wiki", { status: "failed", title: "failed wiki compile e2e" });

    await page.goto("/documents");
    const { region } = await expandTray(page);

    // Cancel the wiki, draft-room, and ingest running rows (aria roles
    // only; each Cancel lives inside its family's listitem).
    await region
      .getByRole("listitem")
      .filter({ hasText: /wiki/i })
      .getByRole("button", { name: /cancel/i })
      .first()
      .click({ timeout: 20_000 });
    await region
      .getByRole("listitem")
      .filter({ hasText: /draft-room/i })
      .getByRole("button", { name: /cancel/i })
      .first()
      .click({ timeout: 20_000 });
    await region
      .getByRole("listitem")
      .filter({ hasText: /ingest/i })
      .getByRole("button", { name: /cancel/i })
      .first()
      .click({ timeout: 20_000 });

    // The stub's job state moved to cancelled — asserted via direct
    // page.request reads, not UI text.
    await expect
      .poll(async () => (await stubJobStatuses(page, "wiki")).includes("cancelled"), {
        timeout: 20_000,
      })
      .toBeTruthy();
    await expect
      .poll(
        async () => (await stubJobStatuses(page, "draft-room")).includes("cancelled"),
        { timeout: 20_000 }
      )
      .toBeTruthy();
    await expect
      .poll(async () => (await stubJobStatuses(page, "ingest")).includes("cancelled"), {
        timeout: 20_000,
      })
      .toBeTruthy();

    // Retry the FAILED wiki row; its status moves to pending/running.
    const failedRow = page
      .getByRole("region", { name: /activity/i })
      .getByRole("listitem")
      .filter({ hasText: /failed wiki compile e2e/i });
    await failedRow.getByRole("button", { name: /retry/i }).click({ timeout: 20_000 });

    await expect
      .poll(
        async () => {
          const statuses = await stubJobStatuses(page, "wiki");
          return statuses.includes("pending") || statuses.includes("running");
        },
        { timeout: 20_000 }
      )
      .toBeTruthy();
  });
});
