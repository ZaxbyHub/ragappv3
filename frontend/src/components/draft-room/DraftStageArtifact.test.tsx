import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DraftStage } from "@/lib/api/draftRoom";

import { DraftStageArtifact } from "./DraftStageArtifact";

function makeStage(overrides: Partial<DraftStage> = {}): DraftStage {
  return {
    id: 1,
    job_id: 5,
    stage: "research",
    attempt: 1,
    status: "completed",
    input_sha256: "a".repeat(64),
    artifact_sha256: "b".repeat(64),
    candidate_sha256: null,
    semantic_changed: false,
    prompt_id: "research-v1",
    prompt_version: "1",
    prompt_sha256: "c".repeat(64),
    model_name: "test-model",
    temperature: 0.2,
    input_tokens: 100,
    output_tokens: 200,
    error_code: null,
    error_message: null,
    started_at: "2026-01-01T00:00:00Z",
    completed_at: "2026-01-01T00:01:00Z",
    artifact: null,
    content_md: null,
    ...overrides,
  };
}

describe("DraftStageArtifact", () => {
  it("renders an honest placeholder when no stage is selected", () => {
    render(<DraftStageArtifact stage={null} />);
    expect(screen.getByText(/no stage selected/i)).toBeInTheDocument();
  });

  it("renders research facets, evidence cards, contradictions, gaps, and retrieval status", () => {
    const stage = makeStage({
      stage: "research",
      artifact: {
        facets: [{ facet_id: "f1", query: "bridge opening date", source_input_ids: [1], rationale: "core fact" }],
        retrieval_status: "partial",
        requested_source_kinds: ["document"],
        successful_source_kinds: ["document"],
        failed_source_kinds: [],
        evidence: [
          {
            label: "S1",
            kind: "document",
            title: "City Records",
            passage: "The bridge opened in 1932.",
            chunk_ref: null,
            observed_at: null,
            retrieval_score: 0.8,
            content_sha256: "d".repeat(64),
          },
        ],
        contradictions: [
          {
            evidence_label_a: "S1",
            evidence_label_b: "S2",
            proposition: "opening year",
            explanation: "S2 says 1933",
          },
        ],
        gaps: [{ description: "No source for architect name", impact: "minor", blocks_drafting: false }],
        source_only: false,
      },
    });
    render(<DraftStageArtifact stage={stage} />);

    const facetsSection = screen.getByRole("heading", { name: "Research facets" }).closest("div") as HTMLElement;
    const evidenceSection = screen.getByRole("heading", { name: "Evidence collected" }).closest("div") as HTMLElement;
    const contradictionsSection = screen
      .getByRole("heading", { name: "Contradictions" })
      .closest("div") as HTMLElement;
    const gapsSection = screen.getByRole("heading", { name: "Gaps" }).closest("div") as HTMLElement;

    expect(screen.getByText(/retrieval: partial/i)).toBeInTheDocument();
    expect(within(facetsSection).getByText("bridge opening date")).toBeInTheDocument();
    expect(within(evidenceSection).getByText(/city records/i)).toBeInTheDocument();
    expect(within(contradictionsSection).getByText(/opening year/i)).toBeInTheDocument();
    expect(within(gapsSection).getByText(/no source for architect name/i)).toBeInTheDocument();
    expect(screen.getByText("View JSON")).toBeInTheDocument();
  });

  it("renders ordered outline sections with mapped evidence and must-keep facts", () => {
    const stage = makeStage({
      stage: "outline",
      artifact: {
        mode: "compose",
        sections: [
          {
            section_id: "s1",
            heading: "Introduction",
            purpose: "Set the scene",
            target_words: 200,
            evidence_labels: ["S1", "S2"],
            must_preserve: ["1932 opening date"],
            acceptance_checks: ["mentions the bridge"],
          },
        ],
        voice_rules: ["formal tone"],
        critic: { verdict: "approved", findings: [] },
      },
    });
    render(<DraftStageArtifact stage={stage} />);

    const sectionItem = screen.getByText(/1\. Introduction/).closest("li") as HTMLElement;
    expect(within(sectionItem).getByText(/mapped evidence:/i)).toBeInTheDocument();
    expect(within(sectionItem).getByText(/S1, S2/)).toBeInTheDocument();
    expect(within(sectionItem).getByText(/must-keep facts:/i)).toBeInTheDocument();
    expect(within(sectionItem).getByText(/1932 opening date/)).toBeInTheDocument();
    expect(screen.getByText(/critic verdict: approved/i)).toBeInTheDocument();
  });

  it("renders lint findings", () => {
    const stage = makeStage({
      stage: "lint",
      artifact: {
        rule_version: "1",
        findings: [
          {
            rule_id: "no-em-dash-abuse",
            severity: "advisory",
            disposition: "open",
            section_id: "s1",
            start: 0,
            end: 5,
            excerpt: "foo—bar",
            message: "Overused em dash.",
          },
        ],
      },
    });
    render(<DraftStageArtifact stage={stage} />);
    const findingItem = screen.getByText("Overused em dash.").closest("li");
    expect(findingItem).not.toBeNull();
    expect(within(findingItem as HTMLElement).getByText(/no-em-dash-abuse/)).toBeInTheDocument();
  });

  it("renders copy/standards edits and whether a semantic change was applied", () => {
    const stage = makeStage({
      stage: "copy",
      artifact: {
        edits: [
          {
            section_id: "s1",
            start: 0,
            end: 10,
            before_sha256: "e".repeat(64),
            after_sha256: "f".repeat(64),
            before_excerpt: "The bridge opened",
            after_excerpt: "The bridge, opened",
            category: "punctuation",
            rationale: "Added a comma for clarity.",
            semantic_change: false,
            affected_claim_ids: [],
            affected_evidence_labels: [],
          },
        ],
        findings: ["No blocking issues."],
      },
    });
    render(<DraftStageArtifact stage={stage} />);
    expect(screen.getByText("No semantic change")).toBeInTheDocument();
    expect(screen.getByText("No blocking issues.")).toBeInTheDocument();

    const standardsStage = makeStage({
      stage: "standards",
      artifact: {
        edits: [
          {
            section_id: "s1",
            start: 0,
            end: 10,
            before_sha256: "e".repeat(64),
            after_sha256: "f".repeat(64),
            before_excerpt: "claims X happened",
            after_excerpt: "sources say X happened",
            category: "attribution",
            rationale: "Added attribution for a high-stakes claim.",
            semantic_change: true,
            affected_claim_ids: ["c1"],
            affected_evidence_labels: ["S1"],
          },
        ],
        findings: [],
      },
    });
    render(<DraftStageArtifact stage={standardsStage} />);
    expect(screen.getByText("Semantic change")).toBeInTheDocument();
  });

  it("renders fact-stage summary counts rather than full claim detail", () => {
    const stage = makeStage({
      stage: "fact",
      artifact: {
        claims: [
          { claim_id: "c1", claim_type: "factual", proposition: "p1", status: "supported", evidence_labels: ["S1"] },
          { claim_id: "c2", claim_type: "factual", proposition: "p2", status: "unsupported", evidence_labels: [] },
        ],
        findings: ["c2 lacks corroborating evidence."],
      },
    });
    render(<DraftStageArtifact stage={stage} />);
    expect(screen.getByText("2 claim(s) checked")).toBeInTheDocument();
    expect(screen.getByText((_, element) => element?.textContent === "supported: 1")).toBeInTheDocument();
    expect(screen.getByText((_, element) => element?.textContent === "unsupported: 1")).toBeInTheDocument();
    expect(screen.getByText("1 finding(s)")).toBeInTheDocument();
  });

  it("renders intake and assemble artifacts structurally", () => {
    const intakeStage = makeStage({
      stage: "intake",
      artifact: {
        brief_hash: "a".repeat(64),
        inputs: [{ input_id: 1, role: "manuscript", raw_sha256: "b".repeat(64), parsed_sha256: "c".repeat(64), character_count: 500 }],
        warnings: [],
      },
    });
    render(<DraftStageArtifact stage={intakeStage} />);
    expect(screen.getByText(/Input #1/)).toBeInTheDocument();

    const assembleStage = makeStage({
      stage: "assemble",
      artifact: {
        revision_id: 8,
        candidate_sha256: "a".repeat(64),
        fact_status: "passed",
        qa_summary: { model_calls: 12 },
      },
    });
    render(<DraftStageArtifact stage={assembleStage} />);
    expect(screen.getByText(/Revision #8/)).toBeInTheDocument();
    expect(screen.getByText("model_calls")).toBeInTheDocument();
  });

  it("falls back to a safe message and the JSON disclosure for a malformed artifact, without throwing", () => {
    const stage = makeStage({ stage: "research", artifact: { unexpected: "shape" } });
    expect(() => render(<DraftStageArtifact stage={stage} />)).not.toThrow();
    expect(screen.getByText(/unrecognised artifact/i)).toBeInTheDocument();
    expect(screen.getByText("View JSON")).toBeInTheDocument();
    expect(screen.getByText(/"unexpected": "shape"/)).toBeInTheDocument();
  });

  it("falls back safely for a non-object artifact (string) without rendering [object Object]", () => {
    const stage = makeStage({ stage: "fact", artifact: "not an object" });
    expect(() => render(<DraftStageArtifact stage={stage} />)).not.toThrow();
    expect(screen.getByText(/unrecognised artifact/i)).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toContain("[object Object]");
  });

  it("shows the stage error when the stage failed", () => {
    const stage = makeStage({
      stage: "fact",
      status: "failed",
      error_code: "model_unavailable",
      error_message: "The configured model was unreachable.",
      artifact: { claims: [], findings: [] },
    });
    render(<DraftStageArtifact stage={stage} />);
    expect(screen.getByText("model_unavailable")).toBeInTheDocument();
    expect(screen.getByText("The configured model was unreachable.")).toBeInTheDocument();
  });
});
