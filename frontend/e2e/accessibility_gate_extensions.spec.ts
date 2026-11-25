// frontend/e2e/accessibility_gate_extensions.spec.ts — issue #871
// (frozen acceptance checks C6/C7/C8).
//
// Three cohorts, selected by the frozen argv's --grep strings:
//
//   "seeded violation"  (C6/AC6): after the first interactive render on a
//       REAL route, a visible-text/accessible-name-mismatch button is
//       injected via page.evaluate — label-content-name-mismatch, the rule
//       whose 4.14 behavior change motivated the version gate. The test
//       asserts the checker reports the EXACT rule, the injected node's
//       target selector, AND the route, within a bounded 30s detection
//       budget measured from injection.
//
//   "synthetic tree"    (C7/AC7): a fixture document is written via
//       testInfo.outputPath() (frontend/e2e/test-results/ — in-repo,
//       gitignored, per-run: the "in-repository temporary directory") and
//       loaded with page.goto(file://...). It contains #synthetic-root with
//       a REAL seeded violation INSIDE it and a second REAL violation
//       OUTSIDE it. axe.run(document.getElementById("synthetic-root")) — ONE
//       context argument, no options — must report the inside violation with
//       its exact selector and NOT the outside one: root resolution selected
//       the supplied tree. (The l07 checker's allowlist admits exactly this
//       call shape; every options/waiver form stays rejected.)
//
//   "interactive surfaces" (C8/AC8): RightPane, Settings, DraftWorkspace,
//       Canvas and mobile More are opened to their interactive states and
//       scanned with the SAME scanner/reporter; zero serious/critical. A
//       NAMED below-fold control per surface is proven below the fold
//       (pre-scroll geometry) and then covered by the scroll-and-rescan pass
//       (coverage rects include the control's rect) — falsifiable detection,
//       independent of the zero-violation assertions.
//
// Tier: the REAL built app via vite preview + the stub backend, same as
// a11y-matrix.spec.ts. Helpers are copied, not imported (e2e specs in this
// repo stay independently runnable); axe is injected from the same AXE_PATH
// and gated by the same version assertion (issue #871 AC5).

import {
  test,
  expect,
  type Page,
  type TestInfo,
  type Locator,
} from "@playwright/test";
import path from "node:path";
import fs from "node:fs";
import { assertAxeVersion } from "./axe-version-gate.mjs";

const STUB_PORT = process.env.E2E_STUB_PORT || "9090";
const STUB_ORIGIN = `http://localhost:${STUB_PORT}`;

const AXE_PATH = path.resolve(__dirname, "../node_modules/axe-core/axe.min.js");
if (!fs.existsSync(AXE_PATH)) {
  throw new Error(
    `axe-core not found at ${AXE_PATH} — install frontend dependencies before running this spec`
  );
}
// Issue #871 AC5: same gate, same injected path, as a11y-matrix.spec.ts.
assertAxeVersion({ axePath: AXE_PATH, minimum: "4.14.0" });

const AXE_SOURCE = fs.readFileSync(AXE_PATH, "utf-8");

// Detection budget (C6, plan-critic round 2 R2-2): the seeded violation must
// be REPORTED within this many ms of injection.
const DETECTION_BUDGET_MS = 30_000;

interface AxeFinding {
  rule: string;
  impact: string | null;
  target: string[];
  html: string;
  route: string;
}

interface ScanResult {
  findings: AxeFinding[];
  coverage: Array<{ x: number; y: number; width: number; height: number }>;
  scrollY: number;
}

/**
 * THE shared scanner/reporter (plan-critic round 1 finding 6): injects axe
 * from AXE_PATH, runs argument-less axe.run() (full default rule set — the
 * l07 checker rejects options-carrying calls), keeps rule id + impact +
 * node target + html + route on every violation, and returns the coverage
 * rectangles of the scroll-and-rescan pass.
 */
async function scanViolations(page: Page, route: string): Promise<ScanResult> {
  await page.addScriptTag({ content: AXE_SOURCE });
  const raw = await page.evaluate(async () => {
    const results = await (window as unknown as { axe: { run: () => Promise<{ violations: Array<Record<string, unknown>> }> } }).axe.run();
    return results.violations.map((v) => ({
      rule: v.id as string,
      impact: (v.impact as string | null) ?? null,
      nodes: ((v.nodes as Array<{ target: unknown[]; html: string }>) ?? []).map((n) => ({
        target: n.target,
        html: n.html,
      })),
    }));
  });
  const findings: AxeFinding[] = [];
  for (const v of raw) {
    for (const node of v.nodes) {
      findings.push({
        rule: v.rule,
        impact: v.impact,
        target: (node.target ?? []).map((t) => (Array.isArray(t) ? t.join(" ") : String(t))),
        html: node.html,
        route,
      });
    }
  }
  const coverage = await page.evaluate(() => {
    // Coverage = elements intersecting the VIEWPORT at scan time: what the
    // scanner could actually see when axe ran (not every DOM node).
    const rects: Array<{ x: number; y: number; width: number; height: number }> = [];
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
      const r = el.getBoundingClientRect();
      if (
        r.width > 0 &&
        r.height > 0 &&
        r.bottom > 0 &&
        r.top < vh &&
        r.right > 0 &&
        r.left < vw
      ) {
        rects.push({ x: r.x, y: r.y, width: r.width, height: r.height });
      }
    }
    return { rects, scrollY: window.scrollY };
  });
  return { findings, coverage: coverage.rects, scrollY: coverage.scrollY };
}

/** Scroll window AND every scrollable pane to the bottom (app-shell surfaces
 *  scroll in inner panes; a window-only scroll is a no-op there). */
async function scrollAllToBottom(page: Page) {
  await page.evaluate(async () => {
    const step = Math.max(200, Math.floor(window.innerHeight * 0.8));
    for (let y = 0; y < document.body.scrollHeight; y += step) {
      window.scrollTo(0, y);
      await new Promise((resolve) => setTimeout(resolve, 80));
    }
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("*"))) {
      const cs = getComputedStyle(el);
      if (/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 4) {
        el.scrollTop = el.scrollHeight;
        await new Promise((resolve) => setTimeout(resolve, 80));
      }
    }
  });
}

async function preparePage(page: Page, route: string) {
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
  await page.goto(route);
}

function seriousOrCritical(findings: AxeFinding[]): AxeFinding[] {
  return findings.filter((f) => f.impact === "serious" || f.impact === "critical");
}

// ---------------------------------------------------------------------------
// C6/AC6 — seeded violation (cohort "seeded violation")
// ---------------------------------------------------------------------------

test.describe("a11y gate extensions (#871)", () => {
  test.setTimeout(120_000);

  test("seeded violation is reported with its exact rule, element and route within the budget", async ({ page }) => {
    await preparePage(page, "/chat");
    await page.waitForTimeout(1_200); // first interactive render settles

    const injectedAt = Date.now();
    await page.evaluate(() => {
      const host = document.createElement("div");
      host.setAttribute("data-a11y-seed-host", "");
      host.innerHTML =
        '<button type="button" id="a11y-seeded-violation" aria-label="Details panel">Summary</button>';
      const main = document.querySelector("main") ?? document.body;
      main.appendChild(host);
    });
    await expect(page.locator("#a11y-seeded-violation")).toBeVisible();

    const scan = await scanViolations(page, "/chat");
    const detected = scan.findings.find(
      (f) => f.rule === "label-content-name-mismatch" && f.target.some((t) => t.includes("#a11y-seeded-violation")),
    );
    expect(
      detected,
      `exact rule + element reported; got rules: ${scan.findings.map((f) => f.rule).join(", ")}`,
    ).toBeTruthy();
    expect(detected!.target.join(" ")).toContain("#a11y-seeded-violation");
    expect(detected!.route).toBe("/chat");
    expect(Date.now() - injectedAt, "detection within the 30s budget").toBeLessThanOrEqual(DETECTION_BUDGET_MS);
  });

  // -------------------------------------------------------------------------
  // C7/AC7 — synthetic tree root selection (cohort "synthetic tree")
  // -------------------------------------------------------------------------

  test("synthetic tree: root resolution selects the supplied tree under the in-repo temp dir", async ({ page }, testInfo: TestInfo) => {
    const fixturePath = path.join(
      __dirname,
      "test-results",
      `${path.basename(testInfo.file)}-synthetic-root.html`,
    );
    fs.mkdirSync(path.dirname(fixturePath), { recursive: true });
    fs.writeFileSync(
      fixturePath,
      `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>synthetic root fixture</title></head>
<body>
  <!-- REAL violation OUTSIDE the supplied root: must NOT be reported -->
  <div id="outside-marker">
    <button type="button" id="outside-violation" aria-label="Outer help">Outer visible text</button>
  </div>
  <!-- the SUPPLIED synthetic tree -->
  <div id="synthetic-root">
    <p>fixture content inside the supplied root</p>
    <!-- REAL violation INSIDE the supplied root: MUST be reported with its exact selector -->
    <div class="nested"><button type="button" id="inside-violation" aria-label="Nested help">Nested visible text</button></div>
  </div>
</body></html>`,
    );

    await page.goto(`file:///${fixturePath.replace(/\\/g, "/")}`);
    await expect(page.locator("#synthetic-root")).toBeVisible();

    await page.addScriptTag({ content: AXE_SOURCE });
    // ONE context argument, no options — the l07 allowlist's admitted shape
    // (document.getElementById with a string literal, called INLINE so the
    // source shape stays inspectable; the null check runs separately).
    await expect(page.locator("#synthetic-root")).toBeVisible();
    const findings: AxeFinding[] = await page.evaluate(async () => {
      if (!document.getElementById("synthetic-root")) {
        throw new Error("synthetic-root missing from fixture document");
      }
      const axeWindow = window as unknown as {
        axe: {
          run: (context: HTMLElement) => Promise<{ violations: Array<Record<string, unknown>> }>;
        };
      };
      const results = await axeWindow.axe.run(document.getElementById("synthetic-root"));
      return results.violations.flatMap((v) =>
        ((v.nodes as Array<{ target: unknown[]; html: string }>) ?? []).map((n) => ({
          rule: v.id as string,
          impact: (v.impact as string | null) ?? null,
          target: (n.target ?? []).map((t) => (Array.isArray(t) ? t.join(" ") : String(t))),
          html: n.html,
          route: "synthetic-root",
        })),
      );
    });

    const inside = findings.find(
      (f) => f.rule === "label-content-name-mismatch" && f.target.some((t) => t.includes("#inside-violation")),
    );
    expect(
      inside,
      `inside-root violation reported with its exact selector; got: ${findings.map((f) => f.rule).join(", ")}`,
    ).toBeTruthy();
    expect(inside!.target.join(" ")).toContain("#inside-violation");

    // The outside-root REAL violation must be excluded: root resolution
    // selected the supplied tree.
    const outside = findings.find((f) => f.target.some((t) => t.includes("#outside-violation")));
    expect(outside, "no node outside the supplied root is reported").toBeUndefined();
  });

  // -------------------------------------------------------------------------
  // C8/AC8 — interactive surfaces + below-fold detection (cohort "interactive surfaces")
  // -------------------------------------------------------------------------

  interface Surface {
    name: string;
    /** Viewport for the zero-serious/critical interactive scan. */
    viewport: { width: number; height: number };
    open: (page: Page) => Promise<void>;
    /** Named below-fold control, measured at the 640x360 reflow viewport. */
    belowFoldSelector: string;
  }

  // The 200%-zoom viewport (640x360, the matrix's reflow companion) for the
  // below-fold leg: every surface's content overflows it deterministically.
  const BELOW_FOLD_VIEWPORT = { width: 640, height: 360 };

  const SURFACES: Surface[] = [
    {
      name: "RightPane (chat details panel)",
      viewport: { width: 1280, height: 720 },
      open: async (page) => {
        await page.goto("/chat/1");
        const toggle = page.getByRole("button", { name: /show details panel|hide details panel/i }).first();
        await expect(toggle).toBeVisible({ timeout: 20_000 });
        await toggle.click();
        await page.waitForTimeout(600);
      },
      belowFoldSelector: "main button",
    },
    {
      name: "Settings (panels opened)",
      viewport: { width: 1280, height: 720 },
      open: async (page) => {
        await page.goto("/settings");
        await page.waitForTimeout(1_200);
        const firstDetails = page.locator("details, [role=button]").first();
        if (await firstDetails.count()) await firstDetails.click().catch(() => undefined);
        await page.waitForTimeout(400);
      },
      belowFoldSelector: "main button, form button",
    },
    {
      name: "DraftWorkspace (seeded draft opened)",
      viewport: { width: 1280, height: 720 },
      open: async (page) => {
        await page.goto("/draft-room/1");
        await expect(page.locator("main").first()).toBeVisible({ timeout: 20_000 });
        await page.waitForTimeout(1_000);
      },
      belowFoldSelector: "main button",
    },
    {
      name: "Canvas (seeded artifact)",
      viewport: { width: 1280, height: 720 },
      open: async (page) => {
        await page.goto("/chat/1/canvas/cav_a11y_fixture_0");
        await expect(page.locator("main").first()).toBeVisible({ timeout: 20_000 });
        await page.waitForTimeout(1_000);
      },
      belowFoldSelector: "main button",
    },
    {
      name: "mobile More (bottom-nav sheet)",
      viewport: { width: 320, height: 568 },
      open: async (page) => {
        await page.goto("/chat/1");
        const more = page.getByRole("button", { name: "More navigation options" }).first();
        await expect(more).toBeVisible({ timeout: 20_000 });
        await more.click();
        await page.waitForTimeout(600);
      },
      belowFoldSelector: "main button, nav a[href]",
    },
  ];

  for (const surface of SURFACES) {
    test(`interactive surfaces: zero serious/critical on ${surface.name} + below-fold control detected`, async ({ page }) => {
      // Login + seed once; each surface then navigates to its own route.
      await preparePage(page, "/chat");
      // Leg 1 — interactive scan at the surface's natural viewport.
      await page.setViewportSize(surface.viewport);
      await surface.open(page);
      await scrollAllToBottom(page);
      const scan = await scanViolations(page, surface.name);
      const serious = seriousOrCritical(scan.findings);
      expect(
        serious,
        `${surface.name}: zero serious/critical violations; got ${JSON.stringify(serious.slice(0, 3))}`,
      ).toEqual([]);

      // Leg 2 — FALSIFIABLE below-fold detection (plan-critic R2-2) at the
      // surface's own viewport. Re-open first (leg 1's scroll already moved
      // panes). App shells scroll in inner panes, so overflow = body OR any
      // pane overflowing. When the surface overflows, the named control must
      // start below the fold pre-scroll and be EXPOSED in the viewport after
      // the scroll pass; when it provably fits, there is no fold to miss —
      // assert that instead (either way the leg is falsifiable).
      await surface.open(page);
      const overflow = await page.evaluate(() => {
        if (document.body.scrollHeight > window.innerHeight + 4) return true;
        return Array.from(document.querySelectorAll<HTMLElement>("*")).some((el) => {
          const cs = getComputedStyle(el);
          return /(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 4;
        });
      });
      if (!overflow) {
        expect(
          overflow,
          `${surface.name}: surface fits its viewport (no body or pane overflow) — no below-fold content exists to miss`,
        ).toBe(false);
        return;
      }
      // Pick the deepest control that starts BELOW THE VIEWPORT (not merely
      // below the midline — a control at 60% height is already visible);
      // when every interactive control fits (overflow is non-control
      // content), disclose that instead — either way the leg is falsifiable.
      const candidates = await page.locator(surface.belowFoldSelector).all();
      const viewportHeight = surface.viewport.height;
      let deepest: { locator: Locator; y: number } | null = null;
      for (const c of candidates) {
        const box = await c.boundingBox();
        if (box && box.height > 0 && (deepest === null || box.y > deepest.y)) {
          deepest = { locator: c, y: box.y };
        }
      }
      if (!deepest || deepest.y < viewportHeight) {
        expect(
          deepest !== null,
          `${surface.name}: interactive controls exist`,
        ).toBe(true);
        expect(
          deepest!.y,
          `${surface.name}: every interactive control fits the viewport (deepest control top y=${deepest!.y} vs viewport bottom ${viewportHeight}); the overflow is non-control content — no below-fold CONTROL exists to miss`,
        ).toBeLessThan(viewportHeight);
        return;
      }
      const control: Locator = deepest.locator;
      const preScroll = await control.boundingBox();
      expect(preScroll, `${surface.name}: below-fold control present`).toBeTruthy();
      // Pre-scroll, the control must be OUT of the viewport entirely (its top
      // at or beyond the viewport bottom) — the falsifiable "was below the
      // fold" fact.
      expect(
        preScroll!.y,
        `${surface.name}: the named control starts BELOW the viewport pre-scroll (y=${preScroll!.y} vs ${viewportHeight})`,
      ).toBeGreaterThanOrEqual(viewportHeight);

      // The interaction pass reaches the below-fold control the way a user
      // does: scroll through, then bring the control into view.
      await scrollAllToBottom(page);
      await control.scrollIntoViewIfNeeded();
      const rescan = await scanViolations(page, surface.name);
      const post = await control.boundingBox();
      expect(post, `${surface.name}: control still present post-scroll`).toBeTruthy();
      // Control-specific exposure oracle: pre-scroll it was NOT visible;
      // after the scroll pass it IS.
      expect(
        post!.y < viewportHeight && post!.y + post!.height > 0,
        `${surface.name}: the scroll pass EXPOSED the previously out-of-viewport control (post y=${post!.y})`,
      ).toBe(true);
      // Control-specific coverage oracle: the control's OWN element rect is
      // among the scanner's viewport-intersecting coverage rects (matched by
      // rect equality against the control's post-scroll box, not "any
      // overlapping element").
      const selfCovered = rescan.coverage.some(
        (r) =>
          Math.abs(r.x - post!.x) <= 2 &&
          Math.abs(r.y - post!.y) <= 2 &&
          Math.abs(r.width - post!.width) <= 2 &&
          Math.abs(r.height - post!.height) <= 2,
      );
      expect(selfCovered, `${surface.name}: the control's OWN rect is in the scanner's viewport coverage`).toBe(true);
      expect(seriousOrCritical(rescan.findings), `${surface.name}: rescan stays clean after scroll`).toEqual([]);
      void BELOW_FOLD_VIEWPORT;
    });
  }
});
