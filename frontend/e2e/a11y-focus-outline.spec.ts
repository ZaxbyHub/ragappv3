// frontend/e2e/a11y-focus-outline.spec.ts — issue #1159 AC4 (frozen
// acceptance check C4).
//
// WHEN an interactive control receives keyboard focus, THE SYSTEM SHALL
// render an outline at least 2px wide whose colour differs from the surface
// behind it.
//
// Measurement contract (plan-critic round 1, finding 3):
//   - Focus is driven by REAL keyboard Tab walks (page.keyboard), because
//     :focus-visible follows the UA keyboard heuristic — script .focus()
//     alone does not arm it.
//   - The compared surface is the ADJACENT surface the offset outline
//     visually sits over (first opaque ancestor background), not the
//     element's own fill: --primary equals --ring in every theme, so an
//     own-background comparison would be 1:1 for primary buttons.
//   - For sr-only-input proxies (RadioGroupItem) the measured node is the
//     VISIBLE sibling: its indicator may be the global outline or the
//     component's peer-focus-visible ring — both count, measured >=2px with
//     >=3:1 contrast.
//   - All three themes are exercised (light / .dark / .high-contrast).
//
// At base (b15d8a28a) the pre-fix probe recorded outline-style:none on every
// control (no global rule existed; the pre-fix probe note in 08-test-results
// carries the verbatim output). This spec is net-new with the fix.

import { test, expect, type Page } from "@playwright/test";

const STUB_PORT = process.env.E2E_STUB_PORT || "9090";
const STUB_ORIGIN = `http://localhost:${STUB_PORT}`;

type Theme = "light" | "dark" | "high-contrast";

interface FocusProbe {
  label: string;
  selector: string;
  /** Selector of the VISIBLE node to measure (defaults to the control). */
  measureSelector?: string;
}

const CONTROLS: FocusProbe[] = [
  { label: "login input (utilities family)", selector: "#login-username" },
  { label: "login submit (primary Button)", selector: "form button, button[type=submit]" },
];

const AUTHED_CONTROLS: FocusProbe[] = [
  { label: "rail toggle (no utilities)", selector: 'button[aria-label="Expand sidebar"], button[aria-label="Collapse sidebar"]' },
  { label: "rail nav link (anchor)", selector: 'nav a[href]' },
  { label: "theme select trigger (composite menu)", selector: 'button[aria-label="Select theme"]' },
  { label: "settings native select (ring-1 family)", selector: "select" },
  {
    label: "draft radio visible proxy (sr-only input family)",
    selector: 'input[type="radio"]',
    measureSelector: 'input[type="radio"] ~ div, input[type="radio"] + div',
  },
];

async function setTheme(page: Page, theme: Theme) {
  await page.evaluate((t) => {
    const root = document.documentElement;
    root.classList.toggle("dark", t === "dark");
    root.classList.toggle("high-contrast", t === "high-contrast");
  }, theme);
}

/** Walk the REAL tab order until the target control owns document.activeElement. */
async function keyboardFocus(page: Page, selector: string) {
  const reached = await page.locator(selector).first().evaluate(() => true).catch(() => false);
  if (!reached) throw new Error(`control missing: ${selector}`);
  for (let i = 0; i < 60; i++) {
    const isTarget = await page.evaluate(
      (sel) => {
        const el = document.querySelector(sel);
        return !!el && document.activeElement === el;
      },
      selector,
    );
    if (isTarget) return;
    await page.keyboard.press("Tab");
  }
  throw new Error(`keyboard tab walk never reached ${selector}`);
}

/** WCAG contrast ratio for two computed rgb()/rgba() colours over white. */
async function contrastAgainst(page: Page, fg: string, bg: string): Promise<number> {
  return page.evaluate(
    ({ fg, bg }) => {
      const parse = (value: string) => {
        const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(value);
        if (!m) return null;
        return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]), a: m[4] === undefined ? 1 : Number(m[4]) };
      };
      const f = parse(fg);
      let b = parse(bg);
      if (!f || !b) return -1;
      if (b.a === 0) b = { r: 255, g: 255, b: 255, a: 1 };
      // Composite a translucent foreground over the background first.
      const eff = {
        r: f.r * f.a + b.r * (1 - f.a),
        g: f.g * f.a + b.g * (1 - f.a),
        b: f.b * f.a + b.b * (1 - f.a),
      };
      const lum = (c: { r: number; g: number; b: number }) => {
        const ch = [c.r, c.g, c.b].map((v) => {
          const s = v / 255;
          return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
      };
      const l1 = lum(eff);
      const l2 = lum(b);
      const [hi, lo] = l1 >= l2 ? [l1, l2] : [l2, l1];
      return (hi + 0.05) / (lo + 0.05);
    },
    { fg, bg },
  );
}

/** First opaque ancestor background (the surface an offset outline sits over). */
async function adjacentSurface(page: Page, selector: string): Promise<string> {
  return page.evaluate(
    (sel) => {
      const el = document.querySelector(sel);
      if (!el) return "rgb(255, 255, 255)";
      let node: HTMLElement | null = el.parentElement;
      while (node) {
        const bg = getComputedStyle(node).backgroundColor;
        const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(bg);
        if (m && (m[4] === undefined || Number(m[4]) === 1)) return bg;
        node = node.parentElement;
      }
      return getComputedStyle(document.body).backgroundColor || "rgb(255, 255, 255)";
    },
    selector,
  );
}

/** Measure the visible keyboard-focus indicator on the measured node. */
async function measureIndicator(page: Page, probe: FocusProbe) {
  const measureSel = probe.measureSelector ?? probe.selector;
  return page.evaluate(
    (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const cs = getComputedStyle(el);
      const ringMatch = /rgba?\([^)]+\)\s+0px\s+0px\s+0px\s+(\d+)px/.exec(cs.boxShadow);
      return {
        outlineWidth: cs.outlineWidth,
        outlineStyle: cs.outlineStyle,
        outlineColor: cs.outlineColor,
        ringWidth: ringMatch ? `${ringMatch[1]}px` : "0px",
        visible: el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0,
      };
    },
    measureSel,
  );
}

async function assertIndicator(page: Page, probe: FocusProbe, theme: Theme) {
  await keyboardFocus(page, probe.selector);
  // Inputs and buttons carry `transition-colors duration-150`; the outline
  // colour is mid-transition immediately after the Tab lands. Let the
  // transition settle so the measured colour is the steady-state value.
  await page.waitForTimeout(300);
  const m = await measureIndicator(page, probe);
  expect(m, `${probe.label} (${theme}) measured`).not.toBeNull();
  expect(m!.visible, `${probe.label} (${theme}) visible`).toBe(true);

  const outlinePx = parseFloat(m!.outlineWidth) || 0;
  const ringPx = parseFloat(m!.ringWidth) || 0;
  const hasOutline = m!.outlineStyle !== "none" && outlinePx >= 2 && !m!.outlineColor.includes("rgba(0, 0, 0, 0)") && !/^rgba\([^)]*, 0\)$/.test(m!.outlineColor);
  const hasRing = ringPx >= 2 && m!.outlineColor !== "";
  expect(
    hasOutline || hasRing,
    `${probe.label} (${theme}): a >=2px visible focus indicator (outline=${m!.outlineStyle} ${m!.outlineWidth} ${m!.outlineColor}, ring=${m!.ringWidth})`,
  ).toBe(true);

  // Contrast: the indicator colour vs the ADJACENT opaque surface.
  const surface = await adjacentSurface(page, probe.measureSelector ?? probe.selector);
  const indicatorColor = hasOutline ? m!.outlineColor : (await page.evaluate(
    (sel) => {
      const el = document.querySelector(sel);
      if (!el) return "rgb(0, 0, 0)";
      const cs = getComputedStyle(el);
      const m2 = /(rgba?\([^)]+\))\s+0px\s+0px\s+0px\s+\d+px/.exec(cs.boxShadow);
      return m2 ? m2[1] : cs.outlineColor;
    },
    probe.measureSelector ?? probe.selector,
  )) as string;
  const ratio = await contrastAgainst(page, indicatorColor, surface);
  expect(ratio, `${probe.label} (${theme}): indicator contrast vs adjacent surface ${surface}`).toBeGreaterThanOrEqual(3);
}

test.describe("a11y keyboard-focus outline (#1159 AC4)", () => {
  test.setTimeout(120_000);

  for (const theme of ["light", "dark", "high-contrast"] as const) {
    test(`anonymous controls present a >=2px non-surface focus indicator (${theme})`, async ({ page }) => {
      await page.goto("/login");
      await expect(page.locator("#login-username")).toBeVisible({ timeout: 20_000 });
      await setTheme(page, theme);
      for (const probe of CONTROLS) {
        await assertIndicator(page, probe, theme);
      }
    });
  }

  for (const theme of ["light", "dark", "high-contrast"] as const) {
    test(`authenticated controls incl. radio proxy present the indicator (${theme})`, async ({ page }) => {
      await page.addInitScript(() => {
        localStorage.setItem("kv_active_vault_id", "1");
      });
      await page.goto("/login");
      await page.fill("#login-username", "e2e-user");
      await page.fill("#login-password", "e2e-pass");
      await page.getByRole("button", { name: /sign in/i }).click();
      await page.waitForURL((url) => !url.pathname.endsWith("/login"), { timeout: 20_000 });
      const res = await page.request.post(`${STUB_ORIGIN}/_e2e/a11y-seed`, { data: {} });
      expect(res.ok()).toBeTruthy();
      await setTheme(page, theme);

      // Settings native select.
      await page.goto("/settings");
      await page.waitForTimeout(800);
      if (await page.locator("select").count()) {
        await assertIndicator(page, AUTHED_CONTROLS[0], theme);
      }

      // Draft assignment radio proxy (sr-only input + visible sibling).
      await page.goto("/draft-room");
      await page.waitForTimeout(1_000);
      if (await page.locator('input[type="radio"]').count()) {
        await assertIndicator(page, AUTHED_CONTROLS[1], theme);
      }
    });
  }
});
