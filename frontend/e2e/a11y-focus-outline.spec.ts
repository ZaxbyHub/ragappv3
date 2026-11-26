// frontend/e2e/a11y-focus-outline.spec.ts — issue #1159 AC4 (frozen
// acceptance check C4).
//
// WHEN an interactive control receives keyboard focus, THE SYSTEM SHALL
// render an outline at least 2px wide whose colour differs from the surface
// behind it.
//
// Measurement contract (plan-critic round 1, finding 3; feedback round
// 2026-11-26 F-001/F-002/F-007):
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
//     >=3:1 contrast. Ring reading takes the WIDEST box-shadow layer (the
//     actual ring), never the first — ring-offset paints an opaque first
//     layer that is NOT the indicator (feedback round F-007).
//   - All three themes are exercised (light / .dark / .high-contrast) by
//     SEEDING kv-theme through addInitScript (the persisted store key, same
//     as a11y-matrix.spec.ts) BEFORE the first navigation, because every
//     page.goto() builds a fresh document and the theme store re-applies
//     its persisted value on boot (feedback round F-002); the applied
//     theme is asserted on <html> before any measurement.
//   - Every declared AUTHED_CONTROLS probe is measured on a route that
//     really renders it, behind HARD presence assertions — a leg whose
//     control is absent FAILS; nothing skips silently (feedback round
//     F-001). A module-level guard rejects an orphaned probe entry.
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
  { label: "native select (ring-1 family, org role)", selector: 'select[aria-label^="Change role for"]' },
  {
    label: "draft radio visible proxy (sr-only input family)",
    selector: 'input[type="radio"]',
    measureSelector: 'input[type="radio"] ~ div, input[type="radio"] + div',
  },
];

// Feedback round F-001: every declared probe must be measured by exactly
// one leg, and each leg's probes are listed up front so the module-level
// guard can reject an orphaned or duplicated entry at import time.
const LEG_PROBES: Record<string, number[]> = {
  "app shell (/chat)": [0, 1, 2],
  "orgs native select (/admin/organizations, card expanded)": [3],
  "draft create dialog radio (/draft-room)": [4],
};
const COVERED = Object.values(LEG_PROBES).flat().sort();
const EXPECTED_COVERAGE = AUTHED_CONTROLS.map((_, i) => i).sort();
if (JSON.stringify(COVERED) !== JSON.stringify(EXPECTED_COVERAGE)) {
  throw new Error(
    `focus-outline orphan-probe guard: legs cover [${COVERED}] but AUTHED_CONTROLS has ` +
      `[${EXPECTED_COVERAGE}] — every declared probe needs exactly one leg`,
  );
}

/**
 * Seed the persisted theme BEFORE any navigation (kv-theme is the zustand
 * persist key — the store reads { state: { theme }, version: 0 } at boot,
 * probe-a11y.spec.ts / a11y-matrix.spec.ts precedent). Init scripts
 * accumulate and run on EVERY new document, so every page.goto() in the
 * test boots with this theme.
 */
async function seedTheme(page: Page, theme: Theme) {
  await page.addInitScript((t) => {
    localStorage.setItem("kv-theme", JSON.stringify({ state: { theme: t }, version: 0 }));
  }, theme);
}

/** Assert the theme the store actually applied before measuring anything. */
async function assertThemeApplied(page: Page, theme: Theme) {
  const classes = await page.evaluate(() => document.documentElement.className);
  if (theme === "light") {
    expect(
      classes,
      `expected the light theme (no dark/high-contrast class), got: ${classes}`,
    ).not.toMatch(/dark|high-contrast/);
  } else {
    expect(classes, `expected the ${theme} class on <html>, got: ${classes}`).toContain(theme);
  }
}

/**
 * Walk the REAL tab order until SOME element matching `selector` owns
 * document.activeElement. Radio groups use roving tabindex (only the
 * checked item is a tab stop), so the target is ANY matching element --
 * pinning the walk to querySelector's first match would never settle on
 * the checked radio (feedback round: the draft-radio leg).
 */
async function keyboardFocus(page: Page, selector: string) {
  const reached = await page.locator(selector).first().evaluate(() => true).catch(() => false);
  if (!reached) throw new Error(`control missing: ${selector}`);
  for (let i = 0; i < 80; i++) {
    const isTarget = await page.evaluate(
      (sel) => {
        const active = document.activeElement;
        return !!active && Array.from(document.querySelectorAll(sel)).some((el) => el === active);
      },
      selector,
    );
    if (isTarget) return;
    await page.keyboard.press("Tab");
  }
  throw new Error(`keyboard tab walk never reached ${selector}`);
}

/**
 * Measure the focused control's indicator, its ADJACENT surface, and the
 * contrast ratio in ONE browser pass anchored to document.activeElement —
 * the proxy family (RadioGroupItem) measures the visible sibling OF THE
 * FOCUSED control, and radio groups place focus on the roving-tabindex
 * item, so nothing may re-resolve by a global querySelector between the
 * focus walk and the measurement (feedback round).
 *
 * Ring reading collects EVERY `0px 0px 0px Npx` box-shadow layer (F-007:
 * ring-offset paints an opaque first layer that is not the indicator —
 * first-match would false-fail the RadioGroupItem proxy) and derives the
 * VISIBLE band thickness widest − second-widest, so a thin ring behind an
 * offset cannot pass the >=2px check on spread alone (closeout review on
 * F-001).
 */
interface MeasuredIndicator {
  error?: string;
  focusTag?: string;
  visible?: boolean;
  outlineWidth?: string;
  outlineStyle?: string;
  outlineColor?: string;
  ringWidth?: string;
  ringColor?: string;
  surface?: string;
  ratioOutline?: number;
  ratioRing?: number;
}

async function measureFocusedIndicator(page: Page, probe: FocusProbe): Promise<MeasuredIndicator> {
  return page.evaluate(
    ({ selector, measureSelector }) => {
      const parse = (value: string) => {
        const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(value);
        if (!m) return null;
        return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]), a: m[4] === undefined ? 1 : Number(m[4]) };
      };
      const ratioOf = (fg: string, bgValue: string) => {
        const f = parse(fg);
        let b = parse(bgValue);
        if (!f || !b) return -1;
        if (b.a === 0) b = { r: 255, g: 255, b: 255, a: 1 };
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
      };

      const focused = document.activeElement as HTMLElement | null;
      if (!focused) return { error: "no element owns focus after the tab walk" };
      let node: Element | null = focused;
      if (measureSelector) {
        // Proxy family: measure the visible sibling OF THE FOCUSED control.
        node = focused.nextElementSibling;
        if (!node) {
          const parent = focused.parentElement;
          node = parent ? parent.querySelector("div") : null;
        }
        if (!node) return { error: `visible sibling missing for ${selector}` };
      } else if (!focused.matches(selector)) {
        return { error: `activeElement does not match ${selector}` };
      }
      const cs = getComputedStyle(node);
      // Collect every `0px 0px 0px Npx` shadow layer, then derive the VISIBLE
      // ring thickness: with ring-offset the widest layer's spread INCLUDES
      // the offset disc (ring-2 + ring-offset-2 paints 2px + 4px layers), so
      // the visible band is widest − second-widest; a single layer is just
      // the ring itself (closeout review on F-001: the raw spread would let
      // a 1px ring behind a 2px offset pass a naive >=2px check).
      const layers: Array<{ color: string; px: number }> = [];
      const layerRe = /(rgba?\([^)]+\))\s+0px\s+0px\s+0px\s+([\d.]+)px/g;
      let layer: RegExpExecArray | null;
      while ((layer = layerRe.exec(cs.boxShadow)) !== null) {
        layers.push({ color: layer[1], px: parseFloat(layer[2]) });
      }
      layers.sort((a, b) => b.px - a.px);
      const widest = layers[0];
      const ringWidth = widest ? (layers[1] ? widest.px - layers[1].px : widest.px) : 0;
      const ringColor = widest ? widest.color : "";
      // First opaque ancestor background (the surface the offset outline
      // visually sits over), walked from the MEASURED node.
      let surface = "rgb(255, 255, 255)";
      let ancestor: HTMLElement | null = node.parentElement;
      let settled = false;
      while (ancestor) {
        const bg = getComputedStyle(ancestor).backgroundColor;
        const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(bg);
        if (m && (m[4] === undefined || Number(m[4]) === 1)) {
          surface = bg;
          settled = true;
          break;
        }
        ancestor = ancestor.parentElement;
      }
      if (!settled) surface = getComputedStyle(document.body).backgroundColor || surface;
      const rect = node.getBoundingClientRect();
      const outlineTransparent = (() => {
        const c = cs.outlineColor;
        return c.includes("rgba(0, 0, 0, 0)") || /^rgba\([^)]*, 0\)$/.test(c);
      })();
      return {
        focusTag: `${focused.tagName}[${focused.getAttribute("aria-label") ?? focused.id ?? ""}]`,
        visible: rect.width > 0 && rect.height > 0,
        outlineWidth: cs.outlineWidth,
        outlineStyle: cs.outlineStyle,
        outlineColor: cs.outlineColor,
        outlineTransparent,
        ringWidth: `${ringWidth}px`,
        ringColor,
        surface,
        // BOTH candidate ratios are computed; the Node-side assertion picks
        // the one bound to the ACCEPTED indicator (Stage-B review: a leg
        // where the ring is the indicator must not be contrast-graded on an
        // inactive outline color).
        ratioOutline: ratioOf(cs.outlineColor, surface),
        ratioRing: ratioOf(ringColor, surface),
      };
    },
    { selector: probe.selector, measureSelector: probe.measureSelector ?? null },
  );
}

async function assertIndicator(page: Page, probe: FocusProbe, theme: Theme) {
  await keyboardFocus(page, probe.selector);
  // Inputs and buttons carry `transition-colors duration-150`; the outline
  // colour is mid-transition immediately after the Tab lands. Let the
  // transition settle so the measured colour is the steady-state value.
  await page.waitForTimeout(300);
  const m = await measureFocusedIndicator(page, probe);
  expect(m.error, `${probe.label} (${theme}) measured`).toBeUndefined();
  expect(m.visible, `${probe.label} (${theme}) visible`).toBe(true);

  const outlinePx = parseFloat(m.outlineWidth ?? "0") ?? 0;
  const ringPx = parseFloat(m.ringWidth ?? "0") ?? 0;
  const transparent = (c: string) => c.includes("rgba(0, 0, 0, 0)") || /^rgba\([^)]*, 0\)$/.test(c);
  const hasOutline = m.outlineStyle !== "none" && outlinePx >= 2 && !transparent(m.outlineColor ?? "");
  const hasRing = ringPx >= 2 && (m.ringColor ?? "") !== "" && !transparent(m.ringColor ?? "");
  expect(
    hasOutline || hasRing,
    `${probe.label} (${theme}): a >=2px visible focus indicator (outline=${m.outlineStyle} ${m.outlineWidth} ${m.outlineColor}, ring=${m.ringWidth} ${m.ringColor})`,
  ).toBe(true);

  // Contrast: bound to the ACCEPTED indicator's colour vs the ADJACENT
  // opaque surface (Stage-B review F-001/F-007 follow-up: a ring-indicator
  // leg must not be graded on an inactive outline colour).
  const ratio = hasOutline ? m.ratioOutline : m.ratioRing;
  expect(
    ratio,
    `${probe.label} (${theme}): ${hasOutline ? "outline" : "ring"} contrast vs adjacent surface ${m.surface} (focused=${m.focusTag})`,
  ).toBeGreaterThanOrEqual(3);
}

test.describe("a11y keyboard-focus outline (#1159 AC4)", () => {
  test.setTimeout(120_000);

  for (const theme of ["light", "dark", "high-contrast"] as const) {
    test(`anonymous controls present a >=2px non-surface focus indicator (${theme})`, async ({ page }) => {
      await page.goto("/login");
      await expect(page.locator("#login-username")).toBeVisible({ timeout: 20_000 });
      await seedTheme(page, theme);
      await page.reload();
      await assertThemeApplied(page, theme);
      for (const probe of CONTROLS) {
        await assertIndicator(page, probe, theme);
      }
    });
  }

  for (const theme of ["light", "dark", "high-contrast"] as const) {
    test(`authenticated app-shell controls (rail, nav link, theme trigger) present the indicator (${theme})`, async ({ page }) => {
      await seedTheme(page, theme);
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
      await assertThemeApplied(page, theme);

      // HARD presence: a missing control FAILS the leg (feedback round
      // F-001 — the if(count()) guards used to skip silently green).
      for (const index of LEG_PROBES["app shell (/chat)"]) {
        const probe = AUTHED_CONTROLS[index];
        await expect(
          page.locator(probe.selector).first(),
          `${probe.label} must render on the app shell`,
        ).toBeVisible({ timeout: 20_000 });
      }
      for (const index of LEG_PROBES["app shell (/chat)"]) {
        await assertIndicator(page, AUTHED_CONTROLS[index], theme);
      }
    });
  }

  for (const theme of ["light", "dark", "high-contrast"] as const) {
    test(`authenticated native select (org role) presents the indicator (${theme})`, async ({ page }) => {
      await seedTheme(page, theme);
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
      await page.goto("/admin/organizations");
      await assertThemeApplied(page, theme);

      // HARD presence: expanding the seeded org card renders its member
      // rows, one native <select> per non-owner member ("Change role for
      // <username>", OrgsPage NATIVE_SELECT family) — the feedback-round
      // replacement for the settings route, which renders no native select
      // at all. The stub serves the seed-gated members row.
      const expand = page.getByRole("button", { name: "Expand" }).first();
      await expect(expand, "the seeded org card's expand control").toBeVisible({ timeout: 20_000 });
      await expand.click();
      const probe = AUTHED_CONTROLS[3];
      await expect(
        page.locator(probe.selector).first(),
        `${probe.label} must render on /admin/organizations (seeded org member, card expanded)`,
      ).toBeVisible({ timeout: 20_000 });
      await assertIndicator(page, probe, theme);
    });
  }

  for (const theme of ["light", "dark", "high-contrast"] as const) {
    test(`authenticated draft radio proxy presents the indicator (${theme})`, async ({ page }) => {
      await seedTheme(page, theme);
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
      await page.goto("/draft-room");
      await assertThemeApplied(page, theme);

      // HARD presence: the RadioGroupItems (sr-only input + visible sibling
      // — DraftAssignmentForm.tsx RadioGroup "Project mode" field) render
      // inside the NEW DRAFT dialog. Opening the dialog is part of the leg:
      // the create control must be visible and enabled, and the radios must
      // appear — no silent skip (feedback round F-001).
      const newDraft = page.getByRole("button", { name: "New draft" }).first();
      await expect(newDraft, "the New draft control on /draft-room").toBeVisible({ timeout: 20_000 });
      await expect(newDraft).toBeEnabled({ timeout: 20_000 });
      await newDraft.click();
      const probe = AUTHED_CONTROLS[4];
      await expect(
        page.locator(probe.selector).first(),
        `${probe.label} must render inside the opened New draft dialog`,
      ).toBeVisible({ timeout: 20_000 });
      await assertIndicator(page, probe, theme);
    });
  }
});
