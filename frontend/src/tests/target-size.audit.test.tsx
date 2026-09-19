// frontend/src/tests/target-size.audit.test.tsx
// Issue #573 (AC6) — acceptance check C6: WCAG 2.5.8 target size (minimum)
// for the bespoke icon-only chat controls. DISCRIMINATING — expected RED at
// base commit ae2e15a0 with exactly these four sub-24px controls:
//   Composer.tsx:559  attachment remove     (icon h-3 w-3           ≈ 12x12)
//   WikiCards.tsx:68  open wiki page        (icon h-2.5 w-2.5       ≈ 10x10)
//   KMSCards.tsx:55   open knowledge entry  (icon h-2.5 w-2.5       ≈ 10x10)
//   ui/sheet.tsx:73   SheetContent close    (icon h-4 w-4           ≈ 16x16)
// The two MarkdownMessage CopyButtons (h-6 w-6 = exactly 24x24) already PASS
// at the floor and are pinned so a regression below 24 fails too.
//
// jsdom has no layout engine, so the check computes the effective box from
// the Tailwind tokens present on the rendered element's className (the issue
// explicitly allows "a Vitest DOM measurement"): explicit h-*/w-* tokens win
// (tailwind-merge semantics: the LAST token of each scale in the class list
// wins, matching cn()'s dedupe); otherwise the first descendant svg icon's
// h-*/w-* tokens plus the element's own p-/px-/py- padding estimate the
// content-sized box. Variant-prefixed tokens (hover:…, md:…, pointer-coarse:…)
// are ignored — the audit measures the default (desktop) render.
//
// Controls using the shared Button icon variant (button.tsx: h-10 w-10) are
// out of scope per the issue. Fix guidance: give each failing control an
// explicit h-6 w-6 (24x24 floor) or equivalent padding+icon box.

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { Composer } from "@/components/chat/Composer";
import { WikiCards } from "@/components/chat/WikiCards";
import { KMSCards } from "@/components/chat/KMSCards";
import { MarkdownMessage } from "@/components/chat/MarkdownMessage";

// =============================================================================
// Tailwind token -> CSS px model
// =============================================================================

const SIZE_TOKENS: Record<string, number> = {
  "h-1": 4, "h-2": 8, "h-2.5": 10, "h-3": 12, "h-3.5": 14, "h-4": 16,
  "h-5": 20, "h-6": 24, "h-7": 28, "h-8": 32, "h-9": 36, "h-10": 40, "h-11": 44,
  "w-1": 4, "w-2": 8, "w-2.5": 10, "w-3": 12, "w-3.5": 14, "w-4": 16,
  "w-5": 20, "w-6": 24, "w-7": 28, "w-8": 32, "w-9": 36, "w-10": 40, "w-11": 44,
  "min-w-4": 16, "min-w-5": 20, "min-w-6": 24, "min-w-7": 28, "min-w-8": 32,
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

/** Last explicit value for a size scale (h-/w-/min-w-) in a class list,
 *  honoring tailwind-merge's last-wins dedupe. Arbitrary px classes like
 *  h-[26px] are honored. Variant-prefixed tokens are ignored. */
function lastSizeToken(classes: string, prefix: "h-" | "w-" | "min-w-"): number | null {
  let value: number | null = null;
  for (const token of classes.split(/\s+/)) {
    if (token.includes(":")) continue; // hover:/sm:/pointer-coarse:… variants
    const exact = token === prefix || token.startsWith(prefix) ? token : null;
    if (!exact) continue;
    if (SIZE_TOKENS[token] !== undefined) {
      value = SIZE_TOKENS[token];
      continue;
    }
    const arbitrary = token.match(new RegExp(`^${prefix.replace("-", "-")}\\[(\\d+)px\\]$`));
    if (arbitrary) value = parseInt(arbitrary[1], 10);
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

function boxFromClasses(elementClasses: string, iconClasses: string): { w: number; h: number } {
  const pad = paddingOf(elementClasses);
  const h = lastSizeToken(elementClasses, "h-");
  const w = lastSizeToken(elementClasses, "w-");
  const iconH = lastSizeToken(iconClasses, "h-") ?? 0;
  const iconW = lastSizeToken(iconClasses, "w-") ?? 0;
  const minW = lastSizeToken(elementClasses, "min-w-") ?? 0;
  return {
    h: Math.max(h ?? 0, iconH + pad.y * 2),
    w: Math.max(w ?? 0, iconW + pad.x * 2, minW),
  };
}

function boxOf(el: HTMLElement): { w: number; h: number } {
  const cls = el.getAttribute("class") ?? "";
  const svg = el.querySelector("svg");
  return boxFromClasses(cls, svg ? svg.getAttribute("class") ?? "" : "");
}

function expectMinTarget(el: HTMLElement, where: string) {
  const { w, h } = boxOf(el);
  expect(
    w >= 24 && h >= 24,
    `${where}: computed target ${w}x${h} CSS px < WCAG 2.5.8 minimum 24x24 — give the control an explicit h-6 w-6 (or icon+padding box) of at least 24x24`
  ).toBe(true);
}

// =============================================================================
// Composer attachment remove control (mounted — Composer.draft.test.tsx mocks)
// =============================================================================

const mockChatState = vi.hoisted(() => ({
  input: "",
  inputError: null as string | null,
  activeChatId: null as string | null,
  setInput: vi.fn(),
}));

const mockUpload = vi.hoisted(() => ({
  id: "u1",
  file: new File(["x"], "notes.pdf", { type: "application/pdf" }),
  uploadProgress: 100,
  progress: 100,
  status: "indexed",
  statusSeen: true,
  chunkCount: 3,
}));

vi.mock("@/stores/useChatStore", () => ({
  useChatStore: vi.fn(() => mockChatState),
}));

vi.mock("@/stores/useChatModeStore", () => ({
  useChatModeStore: vi.fn((selector?: (s: any) => unknown) => {
    const state = {
      chatMode: "thinking",
      setChatMode: vi.fn(),
      temperature: 0.7,
      setTemperature: vi.fn(),
      retrievalMode: "auto",
      setRetrievalMode: vi.fn(),
      citationMode: "enabled",
      setCitationMode: vi.fn(),
      metadataFilterDateFrom: "",
      setMetadataFilterDateFrom: vi.fn(),
      metadataFilterDateTo: "",
      setMetadataFilterDateTo: vi.fn(),
      metadataFilterTags: "",
      setMetadataFilterTags: vi.fn(),
      metadataFilterAuthor: "",
      setMetadataFilterAuthor: vi.fn(),
    };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/stores/useLlmHealthStore", () => ({
  useLlmHealthStore: vi.fn((selector?: (s: any) => unknown) => {
    const state = { thinking: true, instant: true, refresh: vi.fn() };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/stores/useSettingsStore", () => ({
  useSettingsStore: vi.fn((selector?: (s: any) => unknown) => {
    const state = { formData: { default_chat_mode: "thinking" } };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn((selector?: (s: any) => unknown) => {
    const state = {
      activeVaultId: 1,
      getActiveVault: () => ({ id: 1, name: "Test Vault", file_count: 1 }),
    };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/stores/useUploadStore", () => ({
  useUploadStore: vi.fn((selector?: (s: any) => unknown) => {
    const state = {
      uploads: [mockUpload],
      chatAttachmentIds: ["u1"],
      addUploads: vi.fn(),
      attachToChat: vi.fn(),
      detachFromChat: vi.fn(),
      removeUpload: vi.fn(),
    };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/hooks/useUploadMonitoring", () => ({
  useUploadMonitoring: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  uploadDocument: vi.fn(),
  getDocumentStatus: vi.fn(),
}));

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({
    getRootProps: () => ({}),
    getInputProps: () => ({}),
    isDragActive: false,
    open: vi.fn(),
  }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

// =============================================================================
// The audit
// =============================================================================

describe("target size audit — bespoke icon-only chat controls >= 24x24 (issue #573 AC6 / C6)", () => {
  it("Composer attachment remove control is at least 24x24", () => {
    render(<Composer onSend={vi.fn()} onStop={vi.fn()} isStreaming={false} />);

    const removeButton = screen.getByRole("button", { name: /^Remove / });
    expect(removeButton).toBeInTheDocument();
    expectMinTarget(
      removeButton,
      "frontend/src/components/chat/Composer.tsx:559 (attachment remove control)"
    );
  });

  it("WikiCards open-wiki-page control is at least 24x24", () => {
    render(
      <MemoryRouter>
        <WikiCards
          wikiRefs={[
            {
              wiki_label: "W1",
              page_id: 7,
              claim_id: null,
              title: "Deep work",
              slug: null,
              page_type: "concept",
              claim_text: null,
              excerpt: "An excerpt long enough to render.",
              confidence: 0.9,
              status: null,
              page_status: null,
              claim_status: null,
              score: 0.9,
              score_type: "rrf",
              source_count: 1,
              provenance_summary: "one source",
            },
          ]}
        />
      </MemoryRouter>
    );

    const openButton = screen.getByRole("button", { name: "Open wiki page Deep work" });
    expectMinTarget(
      openButton,
      "frontend/src/components/chat/WikiCards.tsx:68 (open wiki page control)"
    );
  });

  it("KMSCards open-knowledge-entry control is at least 24x24", () => {
    render(
      <KMSCards
        kmsRefs={[
          {
            kms_label: "K1",
            entry_id: 3,
            slug: null,
            title: "Onboarding",
            summary: "Summary text.",
            excerpt: "Excerpt text.",
            tags: [],
            status: null,
            source_type: null,
            file_id: null,
            score: 0.5,
            score_type: "rrf",
          },
        ]}
      />
    );

    const openButton = screen.getByRole("button", { name: "Open knowledge entry Onboarding" });
    expectMinTarget(
      openButton,
      "frontend/src/components/chat/KMSCards.tsx:55 (open knowledge entry control)"
    );
  });

  // Source-scan (not a mount): the SheetContent close control is rendered
  // internally by the shared Sheet primitive inside a Radix portal — mounting
  // the full Sheet here couples the audit to Radix portal plumbing without
  // changing what is measured, so per the check plan the audit scans
  // ui/sheet.tsx's own Tailwind tokens instead.
  it("ui/sheet.tsx SheetContent close control is at least 24x24 (source-scan)", () => {
    const source = readFileSync(
      resolve(__dirname, "../components/ui/sheet.tsx"),
      "utf-8"
    );
    const closeMatch = source.match(/<SheetPrimitive\.Close\b([^>]*)>/);
    expect(closeMatch, "SheetPrimitive.Close element not found in ui/sheet.tsx").not.toBeNull();

    const attrs = closeMatch![1];
    const clsMatch = attrs.match(/className="([^"]*)"/);
    const closeClasses = clsMatch ? clsMatch[1] : "";

    const afterClose = source.slice((closeMatch!.index ?? 0) + closeMatch![0].length);
    const iconMatch = afterClose.match(/<X\s+className="([^"]*)"/);
    const iconClasses = iconMatch ? iconMatch[1] : "";

    const { w, h } = boxFromClasses(closeClasses, iconClasses);
    expect(
      w >= 24 && h >= 24,
      `frontend/src/components/ui/sheet.tsx:73 (SheetContent close control, source-scan): computed target ${w}x${h} CSS px < WCAG 2.5.8 minimum 24x24 — give the close control an explicit h-6 w-6 (or icon+padding box) of at least 24x24`
    ).toBe(true);
  });

  it("MarkdownMessage code-block CopyButton (with-language header) is at least 24x24 (pinned at the 24px floor)", () => {
    render(
      <MarkdownMessage content={"```js\nconsole.log(1);\n```"} />
    );

    const copyButton = screen.getByRole("button", { name: "Copy code to clipboard" });
    expectMinTarget(
      copyButton,
      "frontend/src/components/chat/MarkdownMessage.tsx:91 (code block CopyButton, with-language header)"
    );
  });

  it("MarkdownMessage code-block CopyButton (no-language overlay) is at least 24x24 (pinned at the 24px floor)", () => {
    render(
      <MarkdownMessage content={"```\nplain code\n```"} />
    );

    const copyButton = screen.getByRole("button", { name: "Copy code to clipboard" });
    expectMinTarget(
      copyButton,
      "frontend/src/components/chat/MarkdownMessage.tsx:108-111 (code block CopyButton, no-language overlay)"
    );
  });
});
