// frontend/src/pages/WikiPageList.l06.test.tsx
// Issue #777 (AC6 / finding TQ-sibling-batch-03-07) — acceptance check C6:
// the per-row selection checkbox in WikiPageList must offer a hit area of at
// least 24x24 CSS px (WCAG 2.2 2.5.8 target size, minimum). FROZEN acceptance
// check authored pre-fix — DISCRIMINATING: expected RED at base commit
// d5982bf5 because the row Checkbox carries className="h-3.5 w-3.5 mt-1
// shrink-0", i.e. 14x14 after tailwind-merge last-wins over the primitive's
// size-4 (16x16), so the failing assertion reads
// "expected 14 to be greater than or equal to 24".
//
// jsdom has no layout engine, so the check computes the effective box from
// the Tailwind h-*/w-*/size-*/min-h-*/min-w-* tokens on the rendered
// element's className — the same token->px model documented in
// frontend/src/tests/target-size.audit.test.tsx (SIZE_TOKENS map with
// h-3.5=14, h-6=24, ...; last-wins per tailwind-merge, which is how cn()
// dedupes; variant-prefixed tokens like data-[state=checked]: and
// focus-visible: are ignored because they carry no geometry). Per the AC,
// if the element itself is under 24 but a wrapping <label> with padding
// provides the hit area, the label is measured instead.
//
// Rendering mirrors frontend/src/pages/WikiPageList.test.tsx: MemoryRouter
// wrapper, vi.mock("@/lib/api") with bulkWikiPageAction, and the Radix Tabs
// mock reshaped to plain divs/buttons (jsdom cannot drive Radix activation).

import { describe, it, expect, vi } from "vitest";
import { render as rtlRender, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

// Mock API module before importing the component.
vi.mock("@/lib/api", () => ({
  bulkWikiPageAction: vi.fn().mockResolvedValue({}),
}));

// Radix Tabs cannot be activated via fireEvent.click in jsdom — same mock
// shape WikiPageList.test.tsx uses (see frontend-testing-gotchas).
vi.mock("@/components/ui/tabs", async () => {
  const React = await import("react");
  const Ctx = React.createContext<(v: string) => void>(() => {});
  return {
    Tabs: ({ onValueChange, children }: any) =>
      React.createElement(Ctx.Provider, { value: onValueChange }, children),
    TabsList: ({ children }: any) => React.createElement("div", null, children),
    TabsTrigger: ({ value, children }: any) => {
      const onValueChange = React.useContext(Ctx);
      return React.createElement(
        "button",
        { role: "tab", onClick: () => onValueChange(value) },
        children
      );
    },
  };
});

import { WikiPageList } from "./WikiPageList";
import type { WikiPage } from "@/lib/api";

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

const makePage = (overrides: Partial<WikiPage> = {}): WikiPage => ({
  id: 1,
  vault_id: 1,
  slug: "page-one",
  title: "Page One",
  page_type: "entity",
  markdown: "",
  summary: "First page summary",
  status: "draft",
  confidence: 0.9,
  created_by: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  last_compiled_at: null,
  claims: [],
  entities: [],
  lint_findings: [],
  ...overrides,
});

// =============================================================================
// Tailwind token -> CSS px model (target-size.audit.test.tsx, extended with
// the size-*/min-h-* scales the Checkbox primitive uses)
// =============================================================================

const SIZE_TOKENS: Record<string, number> = {
  "h-1": 4, "h-2": 8, "h-2.5": 10, "h-3": 12, "h-3.5": 14, "h-4": 16,
  "h-5": 20, "h-6": 24, "h-7": 28, "h-8": 32, "h-9": 36, "h-10": 40, "h-11": 44,
  "w-1": 4, "w-2": 8, "w-2.5": 10, "w-3": 12, "w-3.5": 14, "w-4": 16,
  "w-5": 20, "w-6": 24, "w-7": 28, "w-8": 32, "w-9": 36, "w-10": 40, "w-11": 44,
  "size-3": 12, "size-3.5": 14, "size-4": 16, "size-5": 20, "size-6": 24,
  "size-7": 28, "size-8": 32, "size-9": 36, "size-10": 40, "size-11": 44,
  "min-w-4": 16, "min-w-5": 20, "min-w-6": 24, "min-w-7": 28, "min-w-8": 32,
  "min-h-4": 16, "min-h-5": 20, "min-h-6": 24, "min-h-7": 28, "min-h-8": 32,
};

const PADDING_TOKENS: Record<string, { x: number; y: number }> = {
  "p-0": { x: 0, y: 0 }, "p-0.5": { x: 2, y: 2 }, "p-1": { x: 4, y: 4 },
  "p-1.5": { x: 6, y: 6 }, "p-2": { x: 8, y: 8 }, "p-2.5": { x: 10, y: 10 },
  "p-3": { x: 12, y: 12 },
  "px-0.5": { x: 2, y: 0 }, "px-1": { x: 4, y: 0 }, "px-1.5": { x: 6, y: 0 },
  "px-2": { x: 8, y: 0 }, "px-2.5": { x: 10, y: 0 }, "px-3": { x: 12, y: 0 },
  "py-0.5": { x: 0, y: 2 }, "py-1": { x: 0, y: 4 }, "py-1.5": { x: 0, y: 6 },
  "py-2": { x: 0, y: 8 }, "py-2.5": { x: 0, y: 10 }, "py-3": { x: 0, y: 12 },
};

const HEIGHT_PREFIXES = ["h-", "size-", "min-h-"] as const;
const WIDTH_PREFIXES = ["w-", "size-", "min-w-"] as const;

/** Last explicit value for an axis (h-/size-/min-h- for height, w-/size-/
 *  min-w- for width) in a class list, honoring tailwind-merge's last-wins
 *  dedupe. Arbitrary px classes like h-[26px] are honored. Variant-prefixed
 *  tokens are ignored. */
function axisSize(classes: string, prefixes: readonly string[]): number | null {
  let value: number | null = null;
  for (const token of classes.split(/\s+/).filter(Boolean)) {
    if (token.includes(":")) continue; // hover:/data-[state=checked]:/... variants
    if (SIZE_TOKENS[token] !== undefined && prefixes.some((p) => token.startsWith(p))) {
      value = SIZE_TOKENS[token];
      continue;
    }
    for (const p of prefixes) {
      const arbitrary = token.match(new RegExp(`^${p}\\[(\\d+)px\\]$`));
      if (arbitrary) {
        value = parseInt(arbitrary[1], 10);
        break;
      }
    }
  }
  return value;
}

function paddingOf(classes: string): { x: number; y: number } {
  let pad = { x: 0, y: 0 };
  for (const token of classes.split(/\s+/)) {
    if (token.includes(":")) continue;
    const p = PADDING_TOKENS[token];
    if (p) pad = { x: p.x || pad.x, y: p.y || pad.y };
  }
  return pad;
}

function boxOfClasses(classes: string): { w: number; h: number } {
  return {
    h: axisSize(classes, HEIGHT_PREFIXES) ?? 0,
    w: axisSize(classes, WIDTH_PREFIXES) ?? 0,
  };
}

describe("WikiPageList row selection checkbox hit area (issue #777 AC6 / TQ-sibling-batch-03-07, C6)", () => {
  it("row selection checkbox hit area is at least 24px", () => {
    render(
      <WikiPageList
        pages={[makePage({ id: 1, title: "Page One", slug: "page-one" })]}
        loading={false}
        onSelect={vi.fn()}
        vaultId={7}
      />
    );

    // The per-row checkbox: aria-label "Select <title>", not the select-all.
    // The match must be UNIQUE — a copy edit to the select-all label (e.g.
    // "Select all") would otherwise satisfy this predicate and silently
    // measure the wrong control (#777 feedback hardening).
    const rowCandidates = screen
      .getAllByRole("checkbox")
      .filter(
        (el) =>
          (el.getAttribute("aria-label") ?? "").startsWith("Select ") &&
          el.getAttribute("aria-label") !== "Select all pages"
      );
    if (rowCandidates.length !== 1) {
      throw new Error(
        `source anchor not found: expected exactly 1 per-row selection checkbox, found ${rowCandidates.length}`
      );
    }
    const rowCheckbox = rowCandidates[0];

    let box = boxOfClasses(rowCheckbox.getAttribute("class") ?? "");
    // Per the AC: a wrapping <label> with padding may lawfully provide the
    // hit area — measure the label box (own tokens, else padding + content)
    // instead when the control itself is under 24.
    if (Math.min(box.w, box.h) < 24) {
      const label = rowCheckbox.closest("label");
      if (label) {
        const labelClasses = label.getAttribute("class") ?? "";
        const pad = paddingOf(labelClasses);
        box = {
          h: Math.max(axisSize(labelClasses, HEIGHT_PREFIXES) ?? 0, box.h + pad.y * 2),
          w: Math.max(axisSize(labelClasses, WIDTH_PREFIXES) ?? 0, box.w + pad.x * 2),
        };
      }
    }

    expect(Math.min(box.w, box.h)).toBeGreaterThanOrEqual(24);
  });
});
