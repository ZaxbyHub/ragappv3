import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  CITATION_SKIP_NODE_TYPES,
  MarkdownMessage,
  MarkdownMessageTestInternals,
  parseCitationSegments,
} from "./MarkdownMessage";
import type { Source, UsedMemory } from "@/lib/api";

vi.mock("shiki", () => ({
  createHighlighter: vi.fn(async () => {
    throw new Error("shiki unavailable in markdown fallback tests");
  }),
}));

const clipboardWriteText = vi.fn().mockResolvedValue(undefined);
Object.defineProperty(navigator, "clipboard", {
  configurable: true,
  value: {
    writeText: clipboardWriteText,
  },
});

beforeEach(() => {
  clipboardWriteText.mockClear();
});

const SOURCES: Source[] = [
  { id: "s1", filename: "a.pdf", source_label: "S1" },
  { id: "s2", filename: "b.pdf", source_label: "S2" },
  { id: "s4", filename: "d.pdf", source_label: "S4" },
];

const MEMS: UsedMemory[] = [
  { id: "m1", memory_label: "M1", content: "User likes brevity." },
  { id: "m2", memory_label: "M2", content: "User prefers citations." },
];

describe("parseCitationSegments - sparse [S#] labels", () => {
  it("preserves S2/S4 labels even though only those are cited", () => {
    const { segments, citedSources } = parseCitationSegments(
      "Per [S2] and [S4], we conclude.",
      SOURCES
    );
    const citationSegs = segments.filter((s) => s.type === "citation");
    expect(citationSegs.map((s) => s.sourceName)).toEqual(["S2", "S4"]);
    expect(citedSources.map((s) => s.id)).toEqual(["s2", "s4"]);
  });
});

describe("parseCitationSegments - [M#] memory citations", () => {
  it("recognizes memory labels distinct from source labels", () => {
    const { segments, citedSources, citedMemories } = parseCitationSegments(
      "Doc claim [S1] and memory [M1].",
      SOURCES,
      MEMS
    );
    const memSegs = segments.filter((s) => s.type === "memory_citation");
    expect(memSegs.map((s) => s.memoryLabel)).toEqual(["M1"]);
    expect(citedSources.map((s) => s.id)).toEqual(["s1"]);
    expect(citedMemories.map((m) => m.id)).toEqual(["m1"]);
  });

  it("does not look up memory M1 as document S1", () => {
    const { citedMemories, citedSources } = parseCitationSegments(
      "Memory says [M1].",
      SOURCES,
      MEMS
    );
    expect(citedMemories[0].memory_label).toBe("M1");
    expect(citedSources.length).toBe(0);
  });

  it("memory chip falls back gracefully when label unknown", () => {
    const { segments, citedMemories } = parseCitationSegments(
      "References [M9] unknown.",
      SOURCES,
      MEMS
    );
    const memSegs = segments.filter((s) => s.type === "memory_citation");
    expect(memSegs.length).toBe(1);
    expect(citedMemories.length).toBe(0);
  });
});

describe("parseCitationSegments - legacy [Source: name]", () => {
  it("still resolves filename-based citations", () => {
    const { citedSources } = parseCitationSegments(
      "See [Source: a.pdf] for details.",
      SOURCES
    );
    expect(citedSources.map((s) => s.id)).toEqual(["s1"]);
  });
});

describe("parseCitationSegments - markdown-aware citation collection (UI-046)", () => {
  it("does not derive evidence cards from markers only inside fenced code blocks", () => {
    const { citedSources } = parseCitationSegments(
      '```ts\nconst marker = "[S1]";\n```',
      SOURCES
    );
    expect(citedSources).toEqual([]);
  });

  it("does not derive evidence cards from markers only inside inline code", () => {
    const { citedSources } = parseCitationSegments("Run `[S2]` first.", SOURCES);
    expect(citedSources).toEqual([]);
  });

  it("derives evidence cards from prose markers", () => {
    const { citedSources } = parseCitationSegments("Per [S1], we conclude.", SOURCES);
    expect(citedSources.map((s) => s.id)).toEqual(["s1"]);
  });

  it("collects only the prose marker when prose and code both contain markers", () => {
    const { citedSources } = parseCitationSegments(
      "See [S1] and `code [S2]` for details.",
      SOURCES
    );
    expect(citedSources.map((s) => s.id)).toEqual(["s1"]);
  });

  it("still resolves legacy filename citations in prose", () => {
    const { citedSources } = parseCitationSegments(
      "See [Source: a.pdf] for details.",
      SOURCES
    );
    expect(citedSources.map((s) => s.id)).toEqual(["s1"]);
  });

  it("does not collect legacy filename markers located in fenced code", () => {
    const { citedSources } = parseCitationSegments("```\n[Source: a.pdf]\n```", SOURCES);
    expect(citedSources).toEqual([]);
  });

  it("collects markers inside a markdown link's text (reachable prose child)", () => {
    const { citedSources } = parseCitationSegments(
      "Click [hello [S1] world](https://example.com) now.",
      SOURCES
    );
    expect(citedSources.map((s) => s.id)).toEqual(["s1"]);
  });

  it("does not collect markers from a link title or an HTML comment", () => {
    const linkTitle = parseCitationSegments(
      '[t](https://example.com "title [S1]")',
      SOURCES
    );
    expect(linkTitle.citedSources).toEqual([]);
    const htmlComment = parseCitationSegments("<!-- [S1] -->", SOURCES);
    expect(htmlComment.citedSources).toEqual([]);
  });
});

describe("CITATION_SKIP_NODE_TYPES", () => {
  it("pins the shared skip list consumed by the remark plugin and the label walker", () => {
    expect([...CITATION_SKIP_NODE_TYPES]).toEqual(["code", "inlineCode"]);
  });
});

describe("MarkdownMessage citation chip focus anchor (issue #508)", () => {
  it("stamps data-citation-chip-message on document citation chip wrappers", () => {
    render(<MarkdownMessage content="Per [S1]." sources={SOURCES} messageId="msg-7" />);

    const chip = document.querySelector('[data-citation-chip-message="msg-7"]');
    expect(chip).not.toBeNull();
    // Programmatically focusable so ChatShell's focus restore can land on it.
    expect(chip).toHaveAttribute("tabindex", "-1");
  });

  it("omits the anchor attribute when no messageId is provided", () => {
    render(<MarkdownMessage content="Per [S1]." sources={SOURCES} />);

    expect(document.querySelector("[data-citation-chip-message]")).toBeNull();
  });
});

describe("MarkdownMessage code-located citation markers", () => {
  it("renders a prose marker as a citation chip but leaves code-located markers literal", () => {
    render(<MarkdownMessage content={"Per [S1] and `code [S2]`."} sources={SOURCES} />);

    expect(screen.getAllByLabelText(/Source S1: a\.pdf/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByLabelText(/Source S2: b\.pdf/i)).not.toBeInTheDocument();
    // The code-located marker stays literal text inside the code element.
    expect(screen.getByText("code [S2]").tagName).toBe("CODE");
  });

  it("does not render a citation chip when the marker appears only in fenced code", () => {
    render(<MarkdownMessage content={'```ts\nconst marker = "[S1]";\n```'} sources={SOURCES} />);

    expect(screen.queryByLabelText(/Source S1: a\.pdf/i)).not.toBeInTheDocument();
    expect(screen.getByText('const marker = "[S1]";')).toBeInTheDocument();
  });
});

describe("MarkdownMessage code rendering", () => {
  it("renders inline code without code-block copy controls", () => {
    render(<MarkdownMessage content="Run `npm test` before shipping." />);

    expect(screen.getByText("npm test").tagName).toBe("CODE");
    expect(screen.queryByLabelText("Copy code to clipboard")).not.toBeInTheDocument();
  });

  it("renders fenced code with a language badge and copy control", () => {
    render(<MarkdownMessage content={"```ts\nconst answer = 42;\n```"} />);

    expect(screen.getByText("ts")).toBeInTheDocument();
    expect(screen.getByText("const answer = 42;")).toBeInTheDocument();
    expect(screen.getByLabelText("Copy code to clipboard")).toBeInTheDocument();
  });

  it("renders fenced code without a language as a copyable code block", () => {
    render(<MarkdownMessage content={"```\nplain block\n```"} />);

    expect(screen.getByText("plain block")).toBeInTheDocument();
    expect(screen.getByLabelText("Copy code to clipboard")).toBeInTheDocument();
  });

  it("copies fenced code without the markdown parser trailing newline", async () => {
    render(<MarkdownMessage content={"```txt\nline one\nline two\n```"} />);

    fireEvent.click(screen.getByLabelText("Copy code to clipboard"));

    await waitFor(() => {
      expect(clipboardWriteText).toHaveBeenCalledWith("line one\nline two");
    });
  });

  it("escapes code when the Shiki fallback renderer is used", async () => {
    render(<MarkdownMessage content={'```html\n<img src=x onerror="alert(1)">\n```'} />);

    await waitFor(() => {
      expect(document.querySelector(".shiki-wrapper")).toBeInTheDocument();
    });
    expect(document.querySelector(".shiki-wrapper img")).not.toBeInTheDocument();
    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
  });

  it("joins array code children without inserting commas", () => {
    expect(MarkdownMessageTestInternals.codeChildrenToText(["line1", "line2"])).toBe("line1line2");
  });
});
