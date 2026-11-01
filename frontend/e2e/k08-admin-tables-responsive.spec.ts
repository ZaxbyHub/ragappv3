// frontend/e2e/k08-admin-tables-responsive.spec.ts — issue #771 acceptance
// checks AC1-AC3 (FROZEN), authored from the AC letters only.
//
// Runs against the REAL built app (vite preview :4173) with the stub
// backend (E2E_STUB_PORT, default :9090 — same tier as chat-smoke.spec.ts
// and a11y-matrix.spec.ts; workers:1 serializes everything). Layout claims
// need Playwright: jsdom has no layout engine.
//
// AC map (issue #771):
//   AC1 -> "admin users row actions are visible at 390px"
//   AC2 -> "admin groups row actions are visible at 390px"
//   AC3 -> "organization member row actions are visible at 390px"
//   (AC4/AC5 are jsdom checks in VaultMembersPanel.k08.test.tsx and
//   VaultGroupAccessPanel.k08.test.tsx; AC6-AC8 live in
//   k08-evidence-sheet-snap.spec.ts; AC9-AC12 are PRESERVING checks pinned
//   in existing files — not re-authored here.)
//
// The three admin tables mount through the shared Table component
// (frontend/src/components/ui/table.tsx renders a "relative w-full
// overflow-auto" wrapper), which only scrolls sideways. The metric below is
// VISIBILITY-FILTERED as the issue's Constraints require (the capture probe
// that justified these offenders is unfiltered): only controls that are
// visible (computed visibility/display, non-zero box) and not inside an
// aria-hidden="true" subtree are counted, and a control counts as clipped
// only when its right edge exceeds Math.min(nearest overflow-x auto/scroll
// ancestor's right, window.innerWidth) at scrollLeft 0 (with the same 1px
// tolerance the repo's a11y gates use).

import { test, expect, type Page } from "@playwright/test";

const STUB_PORT = process.env.E2E_STUB_PORT || "9090";
const STUB_ORIGIN = `http://localhost:${STUB_PORT}`;

async function login(page: Page) {
  await page.goto("/login");
  await page.fill("#login-username", "e2e-user");
  await page.fill("#login-password", "e2e-pass");
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.endsWith("/login"), { timeout: 20_000 });
}

async function seed(page: Page) {
  const res = await page.request.post(`${STUB_ORIGIN}/_e2e/a11y-seed`, { data: {} });
  if (!res.ok()) throw new Error(`stub control /_e2e/a11y-seed failed: ${res.status()} ${await res.text()}`);
}

function userRow(id: number, username: string, is_active = true) {
  return {
    id,
    username,
    full_name: username,
    role: "user",
    is_active,
    created_at: new Date().toISOString(),
  };
}

// Visibility-filtered clipped-control count (see the header comment). The
// scan covers button, select, input, textarea, a and [role=switch] /
// [role=combobox] elements — selects included because the organizations
// member role control (AC3) is a native <select aria-label="Change role for
// <name>">. `labelPattern` is a RegExp source matched against aria-label.
async function countClippedControls(page: Page, labelPattern: string): Promise<{ count: number; labels: string[] }> {
  return page.evaluate((pattern: string) => {
    const re = new RegExp(pattern);
    const nodes = Array.from(
      document.querySelectorAll('button, select, input, textarea, a, [role="switch"], [role="combobox"]')
    );
    const labels: string[] = [];
    for (const el of nodes) {
      const label = el.getAttribute("aria-label") || "";
      if (!re.test(label)) continue;
      const style = window.getComputedStyle(el);
      if (style.visibility === "hidden" || style.display === "none") continue;
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      let hidden = false;
      for (let n = el; n; n = n.parentElement) {
        if (n.getAttribute && n.getAttribute("aria-hidden") === "true") {
          hidden = true;
          break;
        }
      }
      if (hidden) continue;
      let limit = window.innerWidth;
      for (let n = el as HTMLElement; n; n = n.parentElement) {
        const ox = window.getComputedStyle(n).overflowX;
        if (ox === "auto" || ox === "scroll") {
          limit = Math.min(limit, n.getBoundingClientRect().right);
          break;
        }
      }
      if (rect.right > limit + 1) labels.push(label);
    }
    return { count: labels.length, labels };
  }, labelPattern);
}

test.describe("k08 admin tables responsive (issue #771)", () => {
  // AC1 — #771: WHEN /admin/users renders at 390x844 THE SYSTEM SHALL show
  // the first user's status toggle and Delete action inside the viewport,
  // without horizontal scrolling.
  //
  // RED at base (both the status toggle and the Delete action of the first
  // row sit past the table's scroll-box right edge — the audit's triaged
  // 389px sm offender) and GREEN after the fix. Setup: the admin users list
  // API is fulfilled with two users, alice (active) and bob (inactive).
  // Guard: alice's username is visible. Metric per the AC letter: for the
  // controls labelled "Activate|Deactivate user <name>" and
  // "Delete user <name>" that are visible and not aria-hidden, the count of
  // those whose right edge exceeds the scroll-box/viewport limit, toBe(0).
  test("admin users row actions are visible at 390px", async ({ page }) => {
    await seed(page);
    await page.route(/\/api\/users/, async (route) => {
      await route.fulfill({ json: { users: [userRow(2, "alice", true), userRow(3, "bob", false)], total: 2 } });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);
    await page.goto("/admin/users");
    await expect(page.getByText("alice").locator("visible=true").first()).toBeVisible({ timeout: 20_000 });
    // Positive presence guard (plan-critic R1): the metric must not be able
    // to pass vacuously by every target control being absent or hidden — the
    // counted controls must exist visibly BEFORE zero-clipping is asserted.
    await expect(page.locator('[aria-label="Deactivate user alice"] >> visible=true')).toBeVisible();
    await expect(page.locator('[aria-label="Delete user alice"] >> visible=true')).toBeVisible();
    await page.waitForTimeout(500);

    const clipped = await countClippedControls(
      page,
      "^(Activate|Deactivate) user (alice|bob)$|^Delete user (alice|bob)$"
    );
    expect(
      clipped.count,
      `clipped admin-user action controls: ${clipped.count} [${clipped.labels.join(", ")}]`
    ).toBe(0);
  });

  // AC2 — #771: WHEN /admin/groups renders at 390x844 THE SYSTEM SHALL show
  // the first group's Edit and Delete actions inside the viewport, without
  // horizontal scrolling.
  //
  // RED at base (the actions column renders last inside the same scrolling
  // Table wrapper) and GREEN after the fix. Setup: the groups list API is
  // fulfilled with two groups. Guard: the first group's name is visible.
  // Metric: AC1's, applied to "Edit group <name>" and "Delete group <name>".
  test("admin groups row actions are visible at 390px", async ({ page }) => {
    const now = new Date().toISOString();
    await seed(page);
    await page.route(/\/api\/groups/, async (route) => {
      await route.fulfill({
        json: {
          groups: [
            { id: 1, name: "Alpha Team", description: "fixture group one", created_at: now, org_id: null, organization_name: null },
            { id: 2, name: "Beta Team", description: "fixture group two", created_at: now, org_id: null, organization_name: null },
          ],
          total: 2,
          page: 1,
          per_page: 20,
        },
      });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);
    await page.goto("/admin/groups");
    await expect(page.getByText("Alpha Team").locator("visible=true").first()).toBeVisible({ timeout: 20_000 });
    // Positive presence guard (plan-critic R1) — see AC1.
    await expect(page.locator('[aria-label="Edit group Alpha Team"] >> visible=true')).toBeVisible();
    await expect(page.locator('[aria-label="Delete group Alpha Team"] >> visible=true')).toBeVisible();
    await page.waitForTimeout(500);

    const clipped = await countClippedControls(
      page,
      "^(Edit|Delete) group (Alpha Team|Beta Team)$"
    );
    expect(
      clipped.count,
      `clipped admin-group action controls: ${clipped.count} [${clipped.labels.join(", ")}]`
    ).toBe(0);
  });

  // AC3 — #771: WHEN an organization is expanded on /admin/organizations at
  // 390x844 THE SYSTEM SHALL show the first member's role control and
  // Remove action inside the viewport, without horizontal scrolling.
  //
  // RED at base (the per-org members table is nested in the org card and
  // scrolls through the same Table wrapper) and GREEN after the fix.
  // Setup: the organizations list API is fulfilled with one org and the
  // org-members API with two members, the second NOT owner (the owner row
  // renders no role control and a disabled Remove). The spec clicks Expand.
  // Guard: the member usernames are visible. Metric: AC1's, applied to
  // "Change role for <name>" (a native <select>) and "Remove <name> from
  // organization" for the non-owner member.
  test("organization member row actions are visible at 390px", async ({ page }) => {
    const now = new Date().toISOString();
    await seed(page);
    // Route order matters: Playwright lets the LAST matching route win, and
    // the org-list regex also matches the members URL — register the list
    // FIRST and the members route LAST so the members fetch is fulfilled by
    // the members fixture (plan-critic R1; at the old order the guard itself
    // failed and the check was RED for the wrong reason).
    await page.route(/\/api\/organizations/, async (route) => {
      await route.fulfill({
        json: [{ id: 1, name: "e2e-org", description: "fixture org", member_count: 2, vault_count: 1, created_at: now }],
      });
    });
    await page.route(/\/api\/organizations\/1\/members/, async (route) => {
      await route.fulfill({
        json: {
          members: [
            { user_id: 1, username: "e2e-user", full_name: "E2E User", role: "owner", joined_at: now },
            { user_id: 2, username: "bob", full_name: "Bob Smith", role: "admin", joined_at: now },
          ],
        },
      });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);
    await page.goto("/admin/organizations");
    await expect(page.getByRole("heading", { name: "Organizations" })).toBeVisible({ timeout: 20_000 });
    await page.getByRole("button", { name: "Expand" }).click();
    await expect(page.getByText("@bob").locator("visible=true").first()).toBeVisible({ timeout: 20_000 });
    // Positive presence guard (plan-critic R1) — see AC1.
    await expect(page.getByLabel("Change role for bob").locator("visible=true")).toBeVisible();
    await expect(page.locator('[aria-label="Remove bob from organization"] >> visible=true')).toBeVisible();
    await page.waitForTimeout(500);

    const clipped = await countClippedControls(
      page,
      "^(Change role for bob|Remove bob from organization)$"
    );
    expect(
      clipped.count,
      `clipped organization member action controls: ${clipped.count} [${clipped.labels.join(", ")}]`
    ).toBe(0);
  });
});
