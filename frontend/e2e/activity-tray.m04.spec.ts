// frontend/e2e/activity-tray.m04.spec.ts — issue-tracer trace
// 784-activity-tray-shell-job-center, acceptance check C7 (AC7 + the
// Verification section's cancel/retry leg).
//
// Three e2e tests against the REAL built app (vite preview :4173) with the
// stub backend (:9090). Selectors are resilient (aria roles only — no
// brittle CSS), matching the chat-smoke.spec.ts convention.
//
// AMENDED (trace checkpoint, AC_CHANGED_BY_USER — swarm-pr-feedback round
// 853-20261005): (1) every test resets tray seed state via POST
// /_e2e/reset-jobs so exact-count and id-scoped assertions cannot depend on
// seeds leaked from an earlier spec or a reused stub server (PRR-003) and
// the retry poll can no longer be satisfied by a leftover running seed
// (PRR-002); (2) cancel/retry effects are asserted against the SPECIFIC
// seeded job id (the stub echoes it from POST /_e2e/jobs), not family-wide
// (PRR-045); (3) the reload leg asserts each row's PHASE text (PRR-020);
// (4) the header's stub-route contract names the real /draft-room base
// (PRR-047); (5) the expandTray helper drops its guaranteed-no-op fallback
// click (PRR-046).
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
//     * GET  /api/draft-room/drafts           — the draft list (to know
//       which drafts to poll jobs for).
//     * GET  /api/draft-room/drafts/{id}/jobs — draft-room family jobs.
//     * GET  /api/documents/reindex/jobs      — reindex family jobs.
//
//   ACTION routes the tray's Cancel/Retry buttons POST:
//     * POST /api/wiki/jobs/{id}/cancel        — cancelWikiJob
//     * POST /api/wiki/jobs/{id}/retry         — retryWikiJob
//     * POST /api/draft-room/drafts/{id}/jobs/{id}/cancel — cancelDraftJob
//     * POST /api/draft-room/drafts/{id}/jobs/{id}/retry — retryDraftJob
//     * POST /api/documents/{id}/cancel        — cancelDocumentIngest
//       (new client, core.ts, POST /documents/{file_id}/cancel)
//
//   E2E SEEDING CONTROL routes (test-only, like the /_e2e/* routes the
//   #782 specs use):
//     * POST /_e2e/jobs                 — body { family, status?, title? }
//       seeds one job of that family (default status "running") into the
//       stub's job state so the tray has something to list; responds with
//       the seed's { id, family, status }.
//     * GET  /_e2e/jobs?family={family} — returns { jobs: [...] } with each
//       seeded job's id + status, so assertions read stub state directly
//       via page.request instead of UI text.
//     * POST /_e2e/reset-jobs           — clears all tray seeds and the
//       document rows the ingest seeder materialized.
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

/** The stub's seed records for one family (id + status), direct request. */
async function stubSeeds(
  page: Page,
  family: string
): Promise<Array<{ id: number; status: string }>> {
  const res = await page.request.get(
    `${STUB_ORIGIN}/_e2e/jobs?family=${encodeURIComponent(family)}`,
    { timeout: 10_000 }
  );
  if (!res.ok()) return [];
  const { jobs } = (await res.json()) as {
    jobs?: Array<{ id: number; status?: string }>;
  };
  return (jobs ?? []).map((job) => ({ id: job.id, status: String(job.status ?? "") }));
}

/** The status of one specific seeded job (null when absent). */
async function seedStatus(page: Page, family: string, id: number): Promise<string | null> {
  const seeds = await stubSeeds(page, family);
  return seeds.find((job) => job.id === id)?.status ?? null;
}

/** The tray's Activity region and its (poll-populated) rows. */
async function trayRows(page: Page) {
  const region = page.getByRole("region", { name: /activity/i });
  await expect(region).toBeVisible({ timeout: 20_000 });
  return { region, rows: region.getByRole("listitem") };
}

test.describe("activity tray (trace 784-activity-tray-shell-job-center / C7)", () => {
  test("lists all five families after a reload", async ({ page }) => {
    await login(page);
    await resetJobs(page);

    // Seed one running job per family through the stub's control route.
    const seededIds: Record<string, number> = {};
    for (const family of FAMILIES) {
      seededIds[family] = await seedJob(page, family);
    }

    await page.goto("/documents", { waitUntil: "domcontentloaded" });

    // The Activity landmark is a shell surface: visible on /documents, with
    // one row per family carrying its current phase (PRR-020 — "with their
    // current phase" is asserted, not just counted).
    const { region, rows } = await trayRows(page);

    await expect(rows).toHaveCount(5, { timeout: 20_000 });
    for (const family of FAMILIES) {
      const row = rows.filter({ hasText: new RegExp(family, "i") });
      await expect(row).toHaveCount(1, { timeout: 20_000 });
      // wiki/kms/reindex rows render the job status as their phase; the
      // draft-room row renders its active stage; ingest renders the
      // documentProgress phase vocabulary from the stub's seeded doc.
      const phasePattern =
        family === "draft-room" ? /compiling/i : family === "ingest" ? /parsing/i : /running/i;
      await expect(row.getByText(phasePattern)).toBeVisible({ timeout: 20_000 });
    }

    // Reload-survival: the tray lists the server's jobs again after a full
    // page load — the rows are not client-session-only state.
    await page.reload();
    const afterReload = await trayRows(page);
    await expect(afterReload.rows).toHaveCount(5, { timeout: 20_000 });
    for (const family of FAMILIES) {
      await expect(
        afterReload.region.getByText(new RegExp(family, "i")).first()
      ).toBeVisible({ timeout: 20_000 });
    }
  });

  test("cancel and retry reach the stub server", async ({ page }) => {
    await login(page);
    await resetJobs(page);

    // Self-contained seeds (fresh state after reset): one running job per
    // cancellable family plus one FAILED wiki job (distinctive title) for
    // the retry leg. Ids come from the stub's seeding response so every
    // effect assertion is bound to the exact job that was clicked.
    const wikiId = await seedJob(page, "wiki");
    const draftId = await seedJob(page, "draft-room");
    const ingestId = await seedJob(page, "ingest");
    const failedWikiId = await seedJob(page, "wiki", {
      status: "failed",
      title: "failed wiki compile e2e",
    });

    await page.goto("/documents", { waitUntil: "domcontentloaded" });
    const { region } = await trayRows(page);
    await expect(region.getByRole("listitem")).toHaveCount(4, { timeout: 20_000 });

    // Cancel the running wiki, draft-room, and ingest rows (aria roles
    // only; each Cancel lives inside its family's listitem).
    await region
      .getByRole("listitem")
      .filter({ hasText: /^.*wiki.*$/i })
      .filter({ hasNot: page.getByText(/failed wiki compile e2e/i) })
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

    // The stub's job state moved for the EXACT seeded ids — asserted via
    // direct page.request reads, not UI text.
    await expect
      .poll(async () => await seedStatus(page, "wiki", wikiId), { timeout: 20_000 })
      .toBe("cancelled");
    await expect
      .poll(async () => await seedStatus(page, "draft-room", draftId), { timeout: 20_000 })
      .toBe("cancelled");
    await expect
      .poll(async () => await seedStatus(page, "ingest", ingestId), { timeout: 20_000 })
      .toBe("cancelled");

    // Retry the FAILED wiki row; THAT job's status moves to pending — the
    // id-scoped assertion cannot be satisfied by any other seed's state
    // (PRR-002: the family-wide poll was already true at t=0 in suite
    // order).
    const failedRow = page
      .getByRole("region", { name: /activity/i })
      .getByRole("listitem")
      .filter({ hasText: /failed wiki compile e2e/i });
    await failedRow.getByRole("button", { name: /retry/i }).click({ timeout: 20_000 });

    await expect
      .poll(async () => await seedStatus(page, "wiki", failedWikiId), { timeout: 20_000 })
      .toBe("pending");
  });
});
