import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { getSourceBadgeLabel, SourceCards } from "./SourceCards";
import type { Source } from "@/lib/api";

describe("getSourceBadgeLabel", () => {
  it("renders the stable source_label when present (S2 != 1)", () => {
    const source: Source = {
      id: "x",
      filename: "x.pdf",
      source_label: "S2",
    };
    expect(getSourceBadgeLabel(source, 0)).toBe("S2");
  });

  it("renders S4 even when displayed first in the list", () => {
    const source: Source = {
      id: "y",
      filename: "y.pdf",
      source_label: "S4",
    };
    // fallbackIndex would naively show "S1" — we must not fall back.
    expect(getSourceBadgeLabel(source, 0)).toBe("S4");
  });

  it("falls back to ordinal label when source_label missing", () => {
    const source: Source = { id: "z", filename: "z.pdf" };
    expect(getSourceBadgeLabel(source, 0)).toBe("S1");
    expect(getSourceBadgeLabel(source, 2)).toBe("S3");
  });

  it("ignores empty/whitespace source_label", () => {
    const source: Source = {
      id: "z",
      filename: "z.pdf",
      source_label: "  ",
    };
    expect(getSourceBadgeLabel(source, 0)).toBe("S1");
  });
});

describe("SourceCards synthesized badge", () => {
  const noop = () => {};

  it("shows a Synthesized badge and suppresses the relevance label for synthesized sources", () => {
    const synthesized: Source = {
      id: "syn",
      filename: "Synthesized from 2 sources",
      snippet: "A condensed summary drawn from multiple sources.",
      score: 0.05, // would normally render "Highly Relevant" on the distance scale
      score_type: "distance",
      metadata: { synthesized: true },
    };

    render(
      <SourceCards
        sources={[synthesized]}
        onSourceClick={noop}
        onViewAll={noop}
      />
    );

    expect(screen.getByText("Synthesized")).toBeInTheDocument();
    // The borrowed/misleading relevance label must NOT appear for a synthesized source.
    expect(screen.queryByText("Highly Relevant")).not.toBeInTheDocument();
  });

  it("shows the relevance label and no Synthesized badge for a normal source", () => {
    const normal: Source = {
      id: "real",
      filename: "real.pdf",
      snippet: "Real retrieved chunk text.",
      score: 0.05,
      score_type: "distance",
      metadata: {},
    };

    render(
      <SourceCards sources={[normal]} onSourceClick={noop} onViewAll={noop} />
    );

    expect(screen.getByText("Highly Relevant")).toBeInTheDocument();
    expect(screen.queryByText("Synthesized")).not.toBeInTheDocument();
  });
});

describe("SourceCards cited/retrieved badge (issue #508)", () => {
  const noop = () => {};

  const citedSource: Source = { id: "a", filename: "a.pdf", source_label: "S1" };
  const retrievedSource: Source = { id: "b", filename: "b.pdf", source_label: "S2" };

  it("shows Cited for labels in the valid set and Retrieved otherwise", () => {
    render(
      <SourceCards
        sources={[citedSource, retrievedSource]}
        onSourceClick={noop}
        onViewAll={noop}
        validCitationLabels={new Set(["S1"])}
      />
    );

    expect(screen.getByText("Cited")).toBeInTheDocument();
    expect(screen.getByText("Retrieved")).toBeInTheDocument();
    // The badge vocabulary never claims verification.
    expect(screen.queryByText("verified")).not.toBeInTheDocument();
  });

  it("renders no badge when validCitationLabels is absent (old callers unchanged)", () => {
    render(
      <SourceCards sources={[citedSource]} onSourceClick={noop} onViewAll={noop} />
    );

    expect(screen.queryByText("Cited")).not.toBeInTheDocument();
    expect(screen.queryByText("Retrieved")).not.toBeInTheDocument();
  });
});
