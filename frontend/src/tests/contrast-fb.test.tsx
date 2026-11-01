// frontend/src/tests/contrast-fb.test.tsx
// PR #862 feedback round (swarm-pr-review run pr862-20261008) — regression
// pins for the findings the first guardrail cut could not see. Unfrozen
// file: new-check feedback additions follow the new-file pattern so the
// checkpoint-frozen C1-C9 blobs stay byte-identical.
//
// Pinned here:
//   - F-02: TabsTrigger data-[state=active] inset ring-foreground — the
//     selected-state cue (active pill vs strip) collapsed to 1.07/1.31/1.20
//     (light/dark/high-contrast) when TabsList moved to bg-muted; the ring
//     restores >=3:1 vs the strip in every theme (WCAG 1.4.11).
//   - F-03: RightPane tab count spans inherit the trigger color (no hardcoded
//     text-muted-foreground) so the hover pair is accent-foreground on
//     accent (12.56/7.83/6.00), not muted on accent.
//   - F-05: shared Textarea placeholder is the bare muted-foreground token
//     with the ring focus family — the /80 override measured 3.56:1 on card
//     at the base tokens.
//   - F-01: RejectedFilesBanner Dismiss button carries text-foreground —
//     the container's text-warning-foreground on bg-warning/10 computes
//     1.17-1.27:1 in dark after the foreground flip.
//   - F-04: destructive Badge hover is destructive/90 (label 5.07 light /
//     4.74 dark), not /80 (4.24/4.03).
//   - F-08: both WikiPageList selection checkboxes (row + select-all) are
//     >=24px (WCAG 2.5.8).

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { WikiPageList } from "@/pages/WikiPageList";

const here = dirname(fileURLToPath(import.meta.url));
const readSource = (rel: string): string =>
  readFileSync(resolve(here, "..", rel), "utf-8");

type Rgb = [number, number, number];
type ThemeName = "light" | "dark" | "high-contrast";

function parseThemes(css: string): Record<ThemeName, Record<string, Rgb>> {
  const themes: Record<ThemeName, Record<string, Rgb>> = {
    light: {},
    dark: {},
    "high-contrast": {},
  };
  const nameOf = (sel: string): ThemeName | undefined =>
    sel === ":root" ? "light" : sel === ".dark" ? "dark" : sel === ".high-contrast" ? "high-contrast" : undefined;
  const blockRe = /(:root|\.dark|\.high-contrast)\s*\{([^}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = blockRe.exec(css)) !== null) {
    const theme = nameOf(m[1]);
    if (!theme) continue;
    const tokRe = /--([\w-]+):\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%/g;
    let t: RegExpExecArray | null;
    while ((t = tokRe.exec(m[2])) !== null) {
      themes[theme][t[1]] = hslToRgb(parseFloat(t[2]), parseFloat(t[3]), parseFloat(t[4]));
    }
  }
  return themes;
}

function hslToRgb(h: number, s: number, l: number): Rgb {
  const sat = s / 100;
  const lig = l / 100;
  const c = (1 - Math.abs(2 * lig - 1)) * sat;
  const hp = (h % 360) / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  const table: Rgb[] = [
    [c, x, 0],
    [x, c, 0],
    [0, c, x],
    [0, x, c],
    [x, 0, c],
    [c, 0, x],
  ];
  const [r, g, b] = table[Math.floor(hp) % 6];
  const mAdj = lig - c / 2;
  return [r + mAdj, g + mAdj, b + mAdj];
}

function luminance([r, g, b]: Rgb): number {
  const chan = (v: number) =>
    v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b);
}

function contrastRatio(fg: Rgb, bg: Rgb): number {
  const l1 = luminance(fg);
  const l2 = luminance(bg);
  const [hi, lo] = l1 >= l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

const round2 = (v: number) => Math.round(v * 100) / 100;

const THEMES: readonly ThemeName[] = ["light", "dark", "high-contrast"];
const themes = parseThemes(readSource("index.css"));

const token = (theme: ThemeName, name: string): Rgb => {
  const rgb = themes[theme][name];
  expect(rgb, `${theme} --${name}`).toBeDefined();
  return rgb as Rgb;
};

/** First quoted class string in `source` containing `literal` (cn-call aware
 *  enough for these anchors: every audited literal lives in a quoted run). */
function classStringContaining(source: string, literal: string, file: string): string {
  const re = new RegExp(`"([^"\\n]*${literal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}[^"\\n]*)"`);
  const m = re.exec(source);
  if (!m) throw new Error(`source anchor not found in ${file}: ${literal}`);
  return m[1];
}

describe("PR #862 feedback pins (run pr862-20261008)", () => {
  it("F-02: active TabsTrigger carries a ring-foreground selected cue >=3:1 vs the strip in all themes", () => {
    const source = readSource("components/ui/tabs.tsx");
    const trigger = classStringContaining(source, "data-[state=active]:ring-foreground", "tabs.tsx");
    expect(trigger).toContain("data-[state=active]:ring-2");
    // ring-foreground vs the TabsList surface, every theme (WCAG 1.4.11)
    for (const theme of THEMES) {
      const strip = token(theme, "muted");
      expect(round2(contrastRatio(token(theme, "foreground"), strip)), theme).toBeGreaterThanOrEqual(3);
    }
  });

  it("F-03: RightPane tab count spans no longer hardcode text-muted-foreground", () => {
    const source = readSource("components/chat/RightPane.tsx");
    expect(source).not.toContain('<span className="text-[11px] text-muted-foreground">');
    expect(source).not.toContain('<span className="ml-1.5 text-xs text-muted-foreground">');
    // the inherited pairs the spans now ride: rest muted-on-muted, hover
    // accent-foreground on accent — both >=4.5 in all themes
    for (const theme of THEMES) {
      expect(round2(contrastRatio(token(theme, "muted-foreground"), token(theme, "muted"))), theme).toBeGreaterThanOrEqual(4.5);
      expect(round2(contrastRatio(token(theme, "accent-foreground"), token(theme, "accent"))), theme).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("F-05: shared Textarea placeholder is the bare muted-foreground token with the ring family", () => {
    const classes = classStringContaining(
      readSource("components/ui/textarea.tsx"),
      "placeholder:text-muted-foreground",
      "textarea.tsx"
    );
    expect(classes).toContain("placeholder:text-muted-foreground");
    expect(classes).not.toContain("placeholder:text-muted-foreground/80");
    expect(classes).toContain("focus-visible:ring-2 focus-visible:ring-ring");
    for (const theme of THEMES) {
      expect(round2(contrastRatio(token(theme, "muted-foreground"), token(theme, "card"))), theme).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("F-01: RejectedFilesBanner Dismiss carries text-foreground, >=4.5 on the warning wash in all themes", () => {
    const source = readSource("components/documents/RejectedFilesBanner.tsx");
    expect(source).toContain('className="h-7 text-xs text-foreground"');
    // foreground on warning/10 over background — the wash the container paints
    for (const theme of THEMES) {
      const wash = token(theme, "warning").map(
        (v, i) => v * 0.1 + token(theme, "background")[i] * 0.9
      ) as Rgb;
      expect(round2(contrastRatio(token(theme, "foreground"), wash)), theme).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("F-04: destructive Badge hover is destructive/90, label >=4.5 in all themes", () => {
    const classes = classStringContaining(readSource("components/ui/badge.tsx"), "hover:bg-destructive/", "badge.tsx");
    expect(classes).toContain("hover:bg-destructive/90");
    expect(classes).not.toContain("hover:bg-destructive/80");
    for (const theme of THEMES) {
      const surface = token(theme, "destructive").map(
        (v, i) => v * 0.9 + token(theme, "card")[i] * 0.1
      ) as Rgb;
      expect(round2(contrastRatio(token(theme, "destructive-foreground"), surface)), theme).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("F-08: both WikiPageList selection checkboxes (row + select-all) are >=24px", () => {
    const page = {
      id: 1,
      vault_id: 1,
      slug: "page-one",
      title: "Page One",
      page_type: "entity",
      markdown: "",
      summary: "",
      status: "draft",
      confidence: 0.9,
      created_by: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      last_compiled_at: null,
      claims: [],
      entities: [],
    };
    render(
      <MemoryRouter>
        <WikiPageList pages={[page as never]} loading={false} onSelect={() => {}} />
      </MemoryRouter>
    );
    const size = (el: Element): number => {
      // last-wins per axis over the rendered class string (mirrors C6): a
      // later h-N/w-N overrides an earlier size-N from the primitive
      const cls = el.getAttribute("class") ?? "";
      let h: number | undefined;
      let w: number | undefined;
      for (const m of cls.matchAll(/\b(h|w|size)-([\d.]+)\b/g)) {
        const px = parseFloat(m[2]) * 4;
        if (m[1] === "h") h = px;
        else if (m[1] === "w") w = px;
        else {
          h = px;
          w = px;
        }
      }
      expect(h, cls).toBeDefined();
      expect(w, cls).toBeDefined();
      return Math.min(h as number, w as number);
    };
    for (const checkbox of screen.getAllByRole("checkbox")) {
      expect(size(checkbox)).toBeGreaterThanOrEqual(24);
    }
  });
});
