// frontend/e2e/k07-content-width.spec.ts — issue #770 acceptance checks
// (FROZEN), authored from the AC letters only.
//
// Runs against the REAL built app (vite preview :4173) with the stub backend
// (E2E_STUB_PORT, default :9090 — same tier as chat-smoke.spec.ts and
// a11y-matrix.spec.ts; workers:1 serializes everything). Layout claims need
// Playwright: jsdom has no layout engine.
//
// AC map (issue #770):
//   AC1 -> "search content width is capped at 3840px"
//   AC2 -> "memory content width is capped at 3840px"
//   AC3 -> "settings content width is capped at 3840px"
//   AC4 -> "wiki content width is capped at 3840px"
//   AC5 -> "kms content width is capped at 3840px"
//   AC6 -> "admin routes content width is capped at 3840px"
//   AC7 -> "long prose is at most 80ch wide at 3840px"
//   AC8 -> "search results use a multi column grid at 1920px"
//   AC9 -> "memory results use a multi column grid at 1920px"
//   (AC10/AC11/AC12 of #770 are PRESERVING checks pinned in existing
//   vitest/e2e files — not re-authored here.)
//
// Setup contract: the stub serves /api/settings with chat_configured: true
// (its static fixture), so the UnconfiguredChatBanner — which renders inside
// #main-content but outside the capped wrapper — never renders on any route
// visited here, exactly as every AC in this file requires. The search,
// memory, wiki and admin list APIs are fulfilled with page.route because
// the rows they need (six results / six memories / three pages / admin
// fixtures) go beyond what the stub seeds. /memory, /wiki and /kms are
// vault-gated (VaultGate): the spec selects the stub's E2E Vault exactly
// like chat-smoke.spec.ts:26-33 does.

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

// Memory/wiki/KMS surfaces render their "Select a vault" state until a
// concrete vault is active (a fresh context defaults to "All vaults").
// Wiki/KMS render the shared VaultGate first (#782); its overlay button is
// the one on top, while the header carries a second identical button —
// prefer the gate-scoped control when present to avoid strict-mode hits.
async function selectE2eVault(page: Page) {
  const gateBtn = page
    .getByTestId("vault-gate")
    .getByRole("button", { name: /all vaults \(\d+ files total\)/i });
  // The gate mounts asynchronously after navigation; wait for it (or a short
  // budget) before deciding which control to click, else both render and the
  // unscoped locator strict-mode-fails.
  const gated = await gateBtn.waitFor({ state: "visible", timeout: 5_000 }).then(() => true).catch(() => false);
  if (gated) {
    await gateBtn.click();
  } else {
    await page.getByRole("button", { name: /all vaults \(\d+ files total\)/i }).first().click();
  }
  await page.getByRole("menuitem", { name: /e2e vault/i }).click();
  await expect(
    page.getByRole("button", { name: /active vault: e2e vault/i })
  ).toBeVisible({ timeout: 10_000 });
}

function searchResult(id: number, title: string) {
  return {
    type: "document",
    id,
    title,
    snippet: `snippet ${id}`,
    vault_id: 1,
    url_hint: "/documents/1",
    score: 1.0,
  };
}

function memoryRow(id: string, content: string) {
  return { id, content, metadata: { category: "maintenance" }, updated_at: new Date().toISOString() };
}

function wikiPage(id: number, slug: string, title: string) {
  const now = new Date().toISOString();
  return {
    id,
    vault_id: 1,
    slug,
    title,
    page_type: "entity",
    markdown: "Coolant intervals run every 500 hours.",
    summary: "Maintenance cadence for the coolant system.",
    status: "verified",
    confidence: 0.9,
    version: 1,
    created_by: null,
    created_at: now,
    updated_at: now,
    last_compiled_at: null,
    claims: [],
    entities: [],
    lint_findings: [],
  };
}

const SIX_SEARCH_RESULTS = [1, 2, 3, 4, 5, 6].map((i) => searchResult(i, `result-file-${i}.pdf`));
const SIX_MEMORIES = [
  "Memory fixture row one about coolant intervals.",
  "Memory fixture row two about radiator inspection.",
  "Memory fixture row three about pump maintenance.",
  "Memory fixture row four about hose replacement.",
  "Memory fixture row five about belt tension checks.",
  "Memory fixture row six about filter changes.",
];
const THREE_WIKI_PAGES = [
  wikiPage(1, "coolant-maintenance", "Coolant Maintenance Wiki"),
  wikiPage(2, "radiator-inspection", "Radiator Inspection Wiki"),
  wikiPage(3, "pump-servicing", "Pump Servicing Wiki"),
];

// The AC1 metric, shared by AC1-AC6: the maximum getBoundingClientRect()
// .width over visible descendants of #main-content that directly contain a
// non-whitespace text node (TreeWalker over text nodes).
async function measureWidestText(page: Page): Promise<number> {
  return page.evaluate(() => {
    let max = 0;
    const root = document.getElementById("main-content");
    if (!root) return -1;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const seen = new Set<Element>();
    for (; walker.nextNode(); ) {
      const t = walker.currentNode as Text;
      if (!t.textContent?.trim()) continue;
      const el = t.parentElement;
      if (!el || seen.has(el)) continue;
      seen.add(el);
      const style = window.getComputedStyle(el);
      if (style.visibility === "hidden" || style.display === "none") continue;
      const r = el.getBoundingClientRect();
      if (r.width > max) max = r.width;
    }
    return Math.round(max);
  });
}

// The AC7 metric: for each visible p in #main-content with >= 200 text
// characters, divide getBoundingClientRect().width by the width of one "0"
// glyph measured with a temporary <span>0</span> appended inside that
// paragraph (the span inherits the paragraph's computed font). Returns the
// maximum ratio, rounded.
async function measureProseCh(page: Page): Promise<number> {
  return page.evaluate(() => {
    let max = 0;
    const root = document.getElementById("main-content");
    if (!root) return -1;
    const paragraphs = Array.from(root.querySelectorAll("p")).filter(
      (p) => (p.textContent || "").trim().length >= 200
    );
    for (const p of paragraphs) {
      const style = window.getComputedStyle(p);
      if (style.visibility === "hidden" || style.display === "none") continue;
      const span = document.createElement("span");
      span.textContent = "0";
      span.style.position = "absolute";
      p.appendChild(span);
      const ch = span.getBoundingClientRect().width;
      span.remove();
      if (ch > 0) max = Math.max(max, p.getBoundingClientRect().width / ch);
    }
    return Math.round(max);
  });
}

// The AC8/AC9 metric: the number of result cards whose
// getBoundingClientRect().top equals, within 1px, that of the first result
// card. A "result card" is the direct child of the results container that
// contains the result title. The container is found structurally (the
// nearest ancestor of the first title element that contains every title
// element), so the metric survives the container's class changing from a
// single-column stack to a grid. `selector` picks the title-bearing element
// ("a" for search result links, "p" for memory content paragraphs); each
// entry of `texts` must exactly match one visible title element's trimmed
// text. Returns -1 when a title element cannot be found.
async function countCardsInFirstRow(page: Page, selector: string, texts: string[]): Promise<number> {
  return page.evaluate(
    ({ sel, titles }: { sel: string; titles: string[] }) => {
      const visible = (el: Element): boolean => {
        for (let n: Element | null = el; n; n = n.parentElement) {
          const s = window.getComputedStyle(n);
          if (s.visibility === "hidden" || s.display === "none") return false;
        }
        return true;
      };
      const anchors: Element[] = [];
      for (const title of titles) {
        const found = Array.from(
          document.querySelectorAll(`#main-content ${sel}`)
        ).filter((el) => (el.textContent || "").trim() === title && visible(el));
        if (found.length === 0) return -1;
        anchors.push(found[0]);
      }
      // Results container: the nearest ancestor of the first title element
      // that contains every title element.
      let container = anchors[0].parentElement;
      while (container && !anchors.every((a) => container!.contains(a))) {
        container = container.parentElement;
      }
      if (!container) return -1;
      // Card of a title element: the direct child of the container that
      // contains it.
      const cards = anchors.map((a) => {
        let card = a as HTMLElement;
        while (card.parentElement && card.parentElement !== container) card = card.parentElement;
        return card;
      });
      const firstTop = cards[0].getBoundingClientRect().top;
      return cards.filter((c) => Math.abs(c.getBoundingClientRect().top - firstTop) <= 1).length;
    },
    { sel: selector, titles: texts }
  );
}

test.describe("k07 content width (issue #770)", () => {
  // AC1 — #770: WHEN /search?q=manual renders results at 3840x2160 THE
  // SYSTEM SHALL keep every visible text-bearing element inside
  // #main-content at most 2000 CSS px wide.
  //
  // GREEN at base (PageShell's non-chat wrapper carries a width cap) and
  // must stay green after the fix. Setup: the unified search API is
  // fulfilled with six results via page.route. Guard: the first result's
  // title link is visible. Metric: measureWidestText <= 2000.
  test("search content width is capped at 3840px", async ({ page }) => {
    await seed(page);
    await page.route(/\/api\/search\/unified/, async (route) => {
      await route.fulfill({ json: { results: SIX_SEARCH_RESULTS } });
    });
    await page.setViewportSize({ width: 3840, height: 2160 });
    await login(page);
    await page.goto("/search?q=manual");
    await expect(page.getByRole("link", { name: /result-file-1\.pdf/ })).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(500);

    const widest = await measureWidestText(page);
    expect(widest, `widest text-bearing element: ${widest}px`).toBeLessThanOrEqual(2000);
  });

  // AC2 — #770: WHEN /memory renders memories at 3840x2160 THE SYSTEM SHALL
  // keep every visible text-bearing element inside #main-content at most
  // 2000 CSS px wide.
  //
  // GREEN at base and must stay green after the fix. Setup: the memories
  // list API is fulfilled with six entries and the E2E Vault is selected
  // (memory is vault-gated). Guard: the first memory's text is visible.
  // Metric: measureWidestText <= 2000.
  test("memory content width is capped at 3840px", async ({ page }) => {
    await seed(page);
    await page.route(/\/api\/memories/, async (route) => {
      await route.fulfill({ json: { memories: SIX_MEMORIES.map((content, i) => memoryRow(`m${i + 1}`, content)) } });
    });
    await page.setViewportSize({ width: 3840, height: 2160 });
    await login(page);
    await page.goto("/memory");
    await selectE2eVault(page);
    await expect(page.getByText(SIX_MEMORIES[0]).first()).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(500);

    const widest = await measureWidestText(page);
    expect(widest, `widest text-bearing element: ${widest}px`).toBeLessThanOrEqual(2000);
  });

  // AC3 — #770: WHEN /settings renders at 3840x2160 THE SYSTEM SHALL keep
  // every visible text-bearing element inside #main-content at most 2000
  // CSS px wide.
  //
  // GREEN at base and must stay green after the fix. The stub serves
  // /api/settings. Guard: the page title is visible. Metric:
  // measureWidestText <= 2000.
  test("settings content width is capped at 3840px", async ({ page }) => {
    await page.setViewportSize({ width: 3840, height: 2160 });
    await login(page);
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(500);

    const widest = await measureWidestText(page);
    expect(widest, `widest text-bearing element: ${widest}px`).toBeLessThanOrEqual(2000);
  });

  // AC4 — #770: WHEN /wiki renders at 3840x2160 THE SYSTEM SHALL keep every
  // visible text-bearing element inside #main-content at most 2000 CSS px
  // wide.
  //
  // GREEN at base and must stay green after the fix. Setup: the wiki
  // page-list API is fulfilled with three pages via page.route (the seed's
  // single page does not meet the AC's three-page setup) and the E2E Vault
  // is selected (wiki is vault-gated). Guard: the page title is visible.
  // Metric: measureWidestText <= 2000.
  test("wiki content width is capped at 3840px", async ({ page }) => {
    await seed(page);
    await page.route(/\/api\/wiki\/pages/, async (route) => {
      await route.fulfill({ json: { pages: THREE_WIKI_PAGES, page: 1, per_page: 20, total: 3 } });
    });
    await page.setViewportSize({ width: 3840, height: 2160 });
    await login(page);
    await page.goto("/wiki");
    await selectE2eVault(page);
    await expect(page.getByRole("heading", { name: "Wiki" })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText("Coolant Maintenance Wiki").first()).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(500);

    const widest = await measureWidestText(page);
    expect(widest, `widest text-bearing element: ${widest}px`).toBeLessThanOrEqual(2000);
  });

  // AC5 — #770: WHEN /kms renders entries at 3840x2160 THE SYSTEM SHALL
  // keep every visible text-bearing element inside #main-content at most
  // 2000 CSS px wide.
  //
  // GREEN at base and must stay green after the fix; the already-correct
  // /kms 3-column grid is a preserving baseline this AC must not regress.
  // Setup: the seed provides the KMS entry ("Coolant Schedule Entry") and
  // the E2E Vault is selected (kms is vault-gated). Guard: the first entry
  // is visible. Metric: measureWidestText <= 2000.
  test("kms content width is capped at 3840px", async ({ page }) => {
    await seed(page);
    await page.setViewportSize({ width: 3840, height: 2160 });
    await login(page);
    await page.goto("/kms");
    await selectE2eVault(page);
    await expect(page.getByText("Coolant Schedule Entry").first()).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(500);

    const widest = await measureWidestText(page);
    expect(widest, `widest text-bearing element: ${widest}px`).toBeLessThanOrEqual(2000);
  });

  // AC6 — #770: WHEN /admin/users, /admin/groups and /admin/organizations
  // render at 3840x2160 THE SYSTEM SHALL keep every visible text-bearing
  // element inside #main-content at most 2000 CSS px wide on each route.
  //
  // GREEN at base and must stay green after the fix. Setup: the users,
  // groups and organizations list APIs are fulfilled; the stub user is
  // superadmin so the admin routes render. Guard: each route's page title
  // is visible. Metric per the AC letter: the number of the three routes
  // whose widest text-bearing element (AC1's measure) exceeds 2000px,
  // asserted toBe(0).
  test("admin routes content width is capped at 3840px", async ({ page }) => {
    const now = new Date().toISOString();
    await page.route(/\/api\/users/, async (route) => {
      await route.fulfill({
        json: {
          users: [{ id: 2, username: "alice", full_name: "Alice Johnson", role: "user", is_active: true, created_at: now }],
          total: 1,
        },
      });
    });
    await page.route(/\/api\/groups/, async (route) => {
      await route.fulfill({
        json: {
          groups: [{ id: 1, name: "Alpha Team", description: "fixture group", created_at: now, org_id: null, organization_name: null }],
          total: 1,
          page: 1,
          per_page: 20,
        },
      });
    });
    await page.route(/\/api\/organizations/, async (route) => {
      await route.fulfill({
        json: [{ id: 1, name: "e2e-org", description: "fixture org", member_count: 1, vault_count: 1, created_at: now }],
      });
    });
    await page.setViewportSize({ width: 3840, height: 2160 });
    await login(page);

    const routes: Array<{ path: string; title: string }> = [
      { path: "/admin/users", title: "User Management" },
      { path: "/admin/groups", title: "Groups" },
      { path: "/admin/organizations", title: "Organizations" },
    ];
    const overCeiling: string[] = [];
    for (const row of routes) {
      await page.goto(row.path);
      await expect(page.getByRole("heading", { name: row.title })).toBeVisible({ timeout: 20_000 });
      await page.waitForTimeout(500);
      const widest = await measureWidestText(page);
      if (widest > 2000) overCeiling.push(`${row.path}: ${widest}px`);
    }
    expect(overCeiling.join("; "), `admin routes over the 2000px ceiling: ${overCeiling.join("; ")}`).toBe("");
    expect(overCeiling.length).toBe(0);
  });

  // AC7 — #770: WHEN /memory shows a memory of 600 or more characters at
  // 3840x2160 THE SYSTEM SHALL render every visible paragraph of 200 or
  // more characters in #main-content at most 80 ch wide.
  //
  // RED at base (the long memory paragraph measures about 158 ch — a pixel
  // cap alone does not pass this) and GREEN after the fix. Setup: the
  // memories list is fulfilled with one >= 600-character memory (a repeated
  // sentence) plus a short one, and the E2E Vault is selected. Metric per
  // the AC letter (the character-based check review correction C5
  // requires): measureProseCh <= 80. The check measures characters, not the
  // mechanism — a cap combined with AC9's multi-column grid passes without
  // a separate ch utility.
  test("long prose is at most 80ch wide at 3840px", async ({ page }) => {
    await seed(page);
    const long = "Coolant intervals run every 500 hours and the inspection covers the radiator, the pump and the hoses. ".repeat(7);
    await page.route(/\/api\/memories/, async (route) => {
      await route.fulfill({ json: { memories: [memoryRow("m1", long), memoryRow("m2", "short memory")] } });
    });
    await page.setViewportSize({ width: 3840, height: 2160 });
    await login(page);
    await page.goto("/memory");
    await selectE2eVault(page);
    await expect(page.getByText(/Coolant intervals run every 500 hours/).first()).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(500);

    const maxCh = await measureProseCh(page);
    expect(maxCh, `widest long paragraph in ch units: ${maxCh}`).toBeLessThanOrEqual(80);
  });

  // AC8 — #770: WHEN /search?q=manual returns six results at 1920x1080 THE
  // SYSTEM SHALL lay out at least two result cards side by side in one row.
  //
  // RED at base (results are a single-column stack — one card per row) and
  // GREEN after the fix. Setup is as in AC1. Metric per the AC letter: the
  // number of result cards whose top equals, within 1px, that of the first
  // result card, asserted >= 2.
  test("search results use a multi column grid at 1920px", async ({ page }) => {
    await seed(page);
    await page.route(/\/api\/search\/unified/, async (route) => {
      await route.fulfill({ json: { results: SIX_SEARCH_RESULTS } });
    });
    await page.setViewportSize({ width: 1920, height: 1080 });
    await login(page);
    await page.goto("/search?q=manual");
    await expect(page.getByRole("link", { name: /result-file-1\.pdf/ })).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(500);

    const inRow = await countCardsInFirstRow(page, "a", SIX_SEARCH_RESULTS.map((r) => r.title));
    expect(inRow, `result cards side by side in one row: ${inRow}`).toBeGreaterThanOrEqual(2);
  });

  // AC9 — #770: WHEN /memory shows six memories at 1920x1080 THE SYSTEM
  // SHALL lay out at least two memory cards side by side in one row.
  //
  // RED at base (memories are a single-column stack) and GREEN after the
  // fix. Setup is as in AC2 (six memories, E2E Vault selected). Same metric
  // as AC8 applied to memory cards.
  test("memory results use a multi column grid at 1920px", async ({ page }) => {
    await seed(page);
    await page.route(/\/api\/memories/, async (route) => {
      await route.fulfill({ json: { memories: SIX_MEMORIES.map((content, i) => memoryRow(`m${i + 1}`, content)) } });
    });
    await page.setViewportSize({ width: 1920, height: 1080 });
    await login(page);
    await page.goto("/memory");
    await selectE2eVault(page);
    await expect(page.getByText(SIX_MEMORIES[0]).first()).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(500);

    const inRow = await countCardsInFirstRow(page, "p", SIX_MEMORIES);
    expect(inRow, `memory cards side by side in one row: ${inRow}`).toBeGreaterThanOrEqual(2);
  });
});
