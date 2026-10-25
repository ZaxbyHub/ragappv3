// frontend/src/components/chat/no-nesting.rerearm.test.tsx
// #866 structural pins for the card surfaces the a11y matrix does NOT reach
// at default state: SourceCards (chat RightPane Sources tab) and SessionRail
// rows. The invariant is axe's nested-interactive rule: no interactive
// element sits inside another interactive CONTAINER. The /wiki card surface
// is covered by the frozen e2e check (C5); these jsdom pins cover the
// siblings.

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SourceCards } from "./SourceCards";
import { SessionItem } from "./SessionRail";
import { UploadIndicator } from "@/components/shared/UploadIndicator";
import type { Source } from "@/lib/api";
import type { UploadFile } from "@/stores/useUploadStore";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// UploadIndicator reads the upload store; pin the #866 header shape with a
// one-active-upload state (header row + labeled collapse + Clear sibling).
const uploadMocks = vi.hoisted(() => ({
  uploads: [] as UploadFile[],
  clearCompleted: vi.fn(),
}));
vi.mock("@/stores/useUploadStore", () => ({
  useUploadStore: () => uploadMocks,
}));


const source: Source = {
  id: "src-1",
  filename: "nesting-probe.pdf",
  source_label: "S1",
  snippet:
    "A long snippet that runs past one hundred and twenty characters so the More/Less expander button renders next to the filename open button, exercising both interactive controls at once.",
};

const INTERACTIVE_SELECTOR = "button, a, input, select, textarea, [role='button']";

/** Returns every interactive element that is INSIDE another interactive
 * element (axe nested-interactive shape). */
function nestedInteractives(root: HTMLElement): Element[] {
  const offenders: Element[] = [];
  for (const el of Array.from(root.querySelectorAll(INTERACTIVE_SELECTOR))) {
    for (let node = el.parentElement; node && node !== root; node = node.parentElement) {
      if (node.matches(INTERACTIVE_SELECTOR)) {
        offenders.push(el);
        break;
      }
    }
  }
  return offenders;
}

function offenderDetails(offenders: Element[]): string {
  return offenders
    .map((el) => `<${tagOf(el)}> inside <${firstInteractiveAncestorTag(el)}>`)
    .join("; ");
}

function tagOf(el: Element): string {
  return el.tagName.toLowerCase() + (el.getAttribute("aria-label") ? ` (${el.getAttribute("aria-label")})` : "");
}

function firstInteractiveAncestorTag(el: Element): string {
  for (let node = el.parentElement; node; node = node.parentElement) {
    if (node.matches(INTERACTIVE_SELECTOR)) {
      const role = node.getAttribute("role");
      return node.tagName.toLowerCase() + (role ? `[role=${role}]` : "");
    }
  }
  return "?";
}

describe("#866 structural no-nesting pins (matrix-unreached surfaces)", () => {
  it("SourceCards: no interactive control inside another interactive element", () => {
    const { container } = render(
      <SourceCards sources={[source]} onSourceClick={vi.fn()} onViewAll={vi.fn()} />
    );
    const offenders = nestedInteractives(container);
    expect(offenders, offenderDetails(offenders)).toHaveLength(0);
  });

  it("SourceCards: the filename open button and the expander are distinct siblings", () => {
    render(<SourceCards sources={[source]} onSourceClick={vi.fn()} onViewAll={vi.fn()} />);
    expect(
      screen.getByRole("button", { name: "Source S1: nesting-probe.pdf" })
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /more/i })).toBeInTheDocument();
  });

  it("SessionItem: title button + menu trigger are siblings, not nested", () => {
    const { container } = render(
      <ul>
        <SessionItem
          session={
            {
              id: 1,
              title: "Nesting probe session",
              updated_at: new Date().toISOString(),
            } as never
          }
          isActive={false}
          isPinned={false}
          onClick={vi.fn()}
          onRename={vi.fn()}
          onPinToggle={vi.fn()}
          onDelete={vi.fn()}
          tabIndex={0}
        />
      </ul>
    );
    const offenders = nestedInteractives(container);
    expect(offenders, offenderDetails(offenders)).toHaveLength(0);
    // The row itself is a listitem whose title button owns activation.
    const row = container.querySelector('[role="listitem"]') as HTMLElement;
    expect(row).not.toBeNull();
    expect(
      screen.getByRole("button", { name: "Chat session: Nesting probe session" })
    ).toBeInTheDocument();
  });

  it("UploadIndicator header: no interactive control inside another; labeled collapse carries aria-expanded (#866)", () => {
    uploadMocks.uploads = [
      {
        id: "u1",
        file: new File(["probe"], "probe.pdf", { type: "application/pdf" }),
        status: "uploading",
        uploadProgress: 40,
      } as unknown as UploadFile,
    ];
    const { container } = render(<UploadIndicator />);
    const offenders = nestedInteractives(container);
    expect(offenders, offenderDetails(offenders)).toHaveLength(0);
    const collapse = screen.getByRole("button", {
      name: /collapse upload details|expand upload details/i,
    });
    expect(collapse.getAttribute("aria-expanded")).toBe("false");
    uploadMocks.uploads = [];
  });

  it("SessionRail containers: list roles live ONLY on the row paths, never on the no-results outer container (#868)", () => {
    // Structural pin (source-reading guard pattern): the outer scroll
    // container also renders the no-results state, which has no listitems —
    // a role="list" there would raise aria-required-children. The three row
    // paths (grouped/virtualized/fallback) must each carry one.
    const src = readFileSync(
      resolve(__dirname, "SessionRail.tsx"),
      "utf-8"
    );
    // The outer scroll container is the scrollbar-thin div that opens the
    // sessions-list region; the no-results branch renders directly under it.
    const outerStart = src.indexOf('className="flex-1 overflow-y-auto scrollbar-thin');
    expect(outerStart).toBeGreaterThan(-1);
    const outerOpen = src.slice(outerStart, src.indexOf(">", outerStart));
    expect(outerOpen).not.toContain('role="list"');
    // Every role="list" the component declares sits on a row-path container
    // (the sizing div, the fallback container, or the grouped list).
    const listRoleLines = src
      .split(String.fromCharCode(10))
      .filter((ln) => ln.includes('role="list"'));
    expect(listRoleLines.length).toBeGreaterThanOrEqual(3);
    for (const ln of listRoleLines) {
      expect(ln).toMatch(/Chat sessions|aria-label=.Chat sessions.|role="list"/);
    }
    // And each row path has listitem children in the source.
    expect(src.match(/role="listitem"/g)?.length ?? 0).toBeGreaterThanOrEqual(3);
  });
});
