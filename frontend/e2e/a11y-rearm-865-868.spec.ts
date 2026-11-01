// frontend/e2e/a11y-rearm-865-868.spec.ts — issues #865/#866/#867/#868
// acceptance checks (frozen at the Phase 2.5 red checkpoint).
//
// Focused DOM-contract companions to the axe matrix: one test per waived-rule
// fix surface, asserting the STRUCTURE the re-armed axe rules require rather
// than re-running axe itself (the matrix rows are the per-rule AC1/AC4/AC7/
// AC10 checks; these pin the AC2/AC5/AC11 source-of-truth shapes so a future
// waiver cannot reappear as a passing-but-different DOM).
//
// All three tests are RED at the pre-fix base (bf33d57f) and GREEN after the
// planned fixes land; they ship with the fix PR as regression tests.
//
//   1. "inactive tab triggers aria-controls resolve to mounted panels" (#865
//      AC2): every [role="tab"] trigger's aria-controls id must resolve to an
//      element in the DOM. RED at base because ui/tabs.tsx never passes
//      forceMount, so Radix unmounts inactive TabsContent panels and the
//      inactive triggers' aria-controls dangle.
//   2. "wiki page cards are not interactive containers with nested controls"
//      (#866 AC5): no interactive element on /wiki may sit inside another
//      interactive element (the axe nested-interactive invariant, scoped to
//      the page's main landmark), and each seeded card keeps a focusable
//      primary control (the per-card selection checkbox). RED at base because
//      the WikiPageList Card renders role="button" + tabIndex={0} around a
//      Checkbox. The nesting invariant is deliberately fix-shape-agnostic:
//      any conforming fix (title link, title button, separate open button)
//      has zero interactive-inside-interactive nesting.
//   3. "suggested prompts grid uses native list semantics" (#868 AC11): the
//      [aria-label="Suggested prompts"] grid must be a native UL whose direct
//      children are all LI. RED at base because TranscriptPane renders a DIV
//      role="list" with button children.
//
// Same tier as a11y-matrix.spec.ts: the REAL built app via vite preview
// (frontend/dist — run the frontend build first) + the stub backend on
// E2E_STUB_PORT (default :9090). The helpers below mirror the matrix spec's
// login/seed/prepare approach (copied, not imported — e2e specs in this repo
// stay independently runnable); the seeding contract is documented in
// stub-backend.mjs and exercised there by POST /_e2e/a11y-seed.

import { test, expect, type Page } from "@playwright/test";

const STUB_PORT = process.env.E2E_STUB_PORT || "9090";
const STUB_ORIGIN = `http://localhost:${STUB_PORT}`;

// Mirrors the matrix spec: the VaultGate pages (/wiki among them) render
// their "Select a vault" state until a vault is pinned, so every booted
// context pins the stub's seed vault before login.
async function preparePage(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem("kv_active_vault_id", "1");
  });
  await page.goto("/login");
  await page.fill("#login-username", "e2e-user");
  await page.fill("#login-password", "e2e-pass");
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.endsWith("/login"), { timeout: 20_000 });
  const res = await page.request.post(`${STUB_ORIGIN}/_e2e/a11y-seed`, { data: {} });
  if (!res.ok()) {
    throw new Error(`stub control /_e2e/a11y-seed failed: ${res.status()} ${await res.text()}`);
  }
}

// Navigate to a route, wait for its sentinel, then settle: React Query
// refetches can still be in flight and the app polls /api/health forever, so
// a fixed beat is the only bounded option (matrix-spec precedent).
async function visit(page: Page, path: string, sentinel: string) {
  await page.goto(path);
  await expect(page.locator(sentinel).first()).toBeVisible({ timeout: 20_000 });
  await page.waitForTimeout(800);
}

// ---- #865 AC2: aria-controls must resolve (also see ui/tabs.tsx) -----------

interface DanglingControls {
  route: string;
  tab: string;
  controls: string;
}

test.describe("a11y re-arm #865/#866/#868 DOM contracts", () => {
  for (const route of ["/wiki", "/draft-room"]) {
    // Radix unmounts non-active TabsContent panels at the pre-fix base, so
    // the INACTIVE triggers carry aria-controls ids that resolve to nothing —
    // exactly the state an AT user hits when browsing the tab list. After the
    // forceMount fix every id resolves whether or not the tab is active.
    test(`[${route}] inactive tab triggers aria-controls resolve to mounted panels`, async ({ page }) => {
      await preparePage(page);
      await visit(
        page,
        route,
        route === "/wiki" ? "text=Coolant Maintenance Wiki" : "text=A11Y Fixture Draft"
      );
      const dangling = await page.evaluate(() => {
        const out: Array<{ tab: string; controls: string }> = [];
        for (const tab of Array.from(document.querySelectorAll('[role="tab"]'))) {
          const id = tab.getAttribute("aria-controls");
          if (id && !document.getElementById(id)) {
            out.push({ tab: (tab.textContent ?? "").trim(), controls: id });
          }
        }
        return out;
      });
      expect(
        dangling,
        `${route}: tab triggers whose aria-controls id is not in the DOM ` +
          `(dangling: ${dangling.map((d: DanglingControls) => `${d.tab} -> ${d.controls}`).join(", ")})`
      ).toEqual([]);
    });
  }

  // ---- #866 AC5: cards are not interactive containers -----------------------

  test("wiki page cards are not interactive containers with nested controls", async ({ page }) => {
    await preparePage(page);
    await visit(page, "/wiki", "text=Coolant Maintenance Wiki");
    const report = await page.evaluate(() => {
      const INTERACTIVE =
        'a[href], button, input, select, textarea, [role="button"], [role="link"], [role="checkbox"], [role="switch"], [role="radio"], [role="menuitem"], [role="tab"], [role="option"]';
      const isInteractive = (el: Element) => el.matches(INTERACTIVE);
      // Invariant under test: no interactive element may sit inside another
      // interactive element (axe nested-interactive). At the pre-fix base the
      // WikiPageList Card itself carries role="button" + tabindex="0" and
      // wraps the per-card Checkbox; after the fix the card is a plain
      // layout element and the controls are siblings.
      const nested: string[] = [];
      // Prefix EVERY selector alternative with the main-landmark scope (a
      // plain concatenation would scope only the first alternative).
      const scoped = INTERACTIVE.split(", ")
        .map((sel) => `main ${sel}`)
        .join(", ");
      for (const el of Array.from(document.querySelectorAll(scoped))) {
        for (
          let ancestor = el.parentElement;
          ancestor !== null;
          ancestor = ancestor.parentElement
        ) {
          if (isInteractive(ancestor)) {
            nested.push(
              `<${el.tagName.toLowerCase()}> "${(el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 40)}" inside <${ancestor.tagName.toLowerCase()}${ancestor.getAttribute("role") ? ` role=${ancestor.getAttribute("role")}` : ""}>`
            );
            break;
          }
        }
      }
      // Companion pin: every seeded card keeps at least one focusable primary
      // control — the per-card selection checkbox (aria-label "Select <title>"
      // in WikiPageList.tsx). This is green before AND after the fix; it
      // exists so the de-buttoning cannot regress to a fully inert card.
      const perCardCheckbox = document.querySelector(
        '[aria-label="Select Coolant Maintenance Wiki"]'
      );
      return {
        nested,
        hasPerCardCheckbox: perCardCheckbox !== null,
      };
    });
    expect(
      report.nested,
      "/wiki: interactive elements nested inside interactive containers " +
        `(offenders: ${report.nested.join("; ")})`
    ).toEqual([]);
    expect(report.hasPerCardCheckbox, "/wiki: per-card selection checkbox missing").toBe(true);
  });

  // ---- #868 AC11: native list semantics for the prompts grid ----------------

  test("suggested prompts grid uses native list semantics", async ({ page }) => {
    await preparePage(page);
    await visit(page, "/chat", "textarea[aria-label='Message input']");
    const report = await page.evaluate(() => {
      const grid = document.querySelector('[aria-label="Suggested prompts"]');
      if (!grid) return { missing: true as const, tag: "", childTags: [] as string[] };
      return {
        missing: false as const,
        tag: grid.tagName,
        childTags: Array.from(grid.children).map(
          (c) =>
            c.tagName.toLowerCase() +
            (c.getAttribute("role") ? `[role=${c.getAttribute("role")}]` : "")
        ),
      };
    });
    expect(report.missing, "/chat: suggested-prompts grid not rendered").toBe(false);
    expect(
      report.tag,
      `/chat: suggested-prompts grid must be a native <ul> (got <${report.tag.toLowerCase()}>)`
    ).toBe("UL");
    expect(
      report.childTags.every((t) => t === "li"),
      `/chat: suggested-prompts grid children must all be <li> (got [${report.childTags.join(", ")}])`
    ).toBe(true);
  });
});
