import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DraftInspector } from "./DraftInspector";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import type { DraftStage } from "@/lib/api/draftRoom";

vi.mock("./DraftFindingsPanel", () => ({
  DraftFindingsPanel: (props: Record<string, unknown>) => (
    <div data-testid="findings-panel">
      findings:{String(props.draftId)}:{String(props.revisionId)}:{String(props.baseRevisionId)}:
      {String(props.lockVersion)}:{String(props.canDispose)}:{String(props.tier)}
    </div>
  ),
}));
vi.mock("./DraftClaimsPanel", () => ({
  DraftClaimsPanel: (props: Record<string, unknown>) => (
    <div data-testid="claims-panel">claims:{String(props.draftId)}:{String(props.revisionId)}</div>
  ),
}));
vi.mock("./DraftEvidencePanel", () => ({
  DraftEvidencePanel: (props: Record<string, unknown>) => (
    <div data-testid="evidence-panel">evidence:{String(props.draftId)}:{String(props.jobId)}</div>
  ),
}));
vi.mock("./DraftStageArtifact", () => ({
  DraftStageArtifact: (props: { stage: DraftStage | null }) => (
    <div data-testid="artifact-panel">artifact:{props.stage?.stage ?? "none"}</div>
  ),
}));

function makeStage(overrides: Partial<DraftStage> = {}): DraftStage {
  return {
    id: 1,
    job_id: 5,
    stage: "research",
    attempt: 1,
    status: "completed",
    input_sha256: "abc",
    artifact_sha256: "def",
    candidate_sha256: null,
    semantic_changed: false,
    prompt_id: null,
    prompt_version: null,
    prompt_sha256: null,
    model_name: null,
    temperature: null,
    input_tokens: null,
    output_tokens: null,
    error_code: null,
    error_message: null,
    started_at: null,
    completed_at: null,
    artifact: {},
    content_md: null,
    ...overrides,
  };
}

beforeEach(() => {
  useDraftRoomUiStore.setState({ inspectorTab: "findings" });
});

describe("DraftInspector", () => {
  it("renders the findings tab by default with ids, lockVersion, canDispose, and tier threaded through", () => {
    render(
      <DraftInspector
        draftId={7}
        revisionId={12}
        jobId={5}
        stage={makeStage()}
        lockVersion={3}
        canDispose
        tier="high_stakes"
      />
    );
    expect(screen.getByTestId("findings-panel")).toHaveTextContent(
      "findings:7:12:12:3:true:high_stakes"
    );
  });

  it("switches to claims, evidence, and artifact tabs and passes the right ids", async () => {
    const user = userEvent.setup();
    render(
      <DraftInspector
        draftId={7}
        revisionId={12}
        jobId={5}
        stage={makeStage({ stage: "outline" })}
        lockVersion={3}
        canDispose={false}
        tier="standard"
      />
    );

    await user.click(screen.getByRole("tab", { name: "Claims" }));
    expect(screen.getByTestId("claims-panel")).toHaveTextContent("claims:7:12");

    await user.click(screen.getByRole("tab", { name: "Evidence" }));
    expect(screen.getByTestId("evidence-panel")).toHaveTextContent("evidence:7:5");

    await user.click(screen.getByRole("tab", { name: "Stage artifact" }));
    expect(screen.getByTestId("artifact-panel")).toHaveTextContent("artifact:outline");
  });

  it("passes a null stage through to the artifact panel without throwing", async () => {
    const user = userEvent.setup();
    render(
      <DraftInspector
        draftId={7}
        revisionId={null}
        jobId={null}
        stage={null}
        lockVersion={1}
        canDispose={false}
        tier="sensitive"
      />
    );
    await user.click(screen.getByRole("tab", { name: "Stage artifact" }));
    expect(screen.getByTestId("artifact-panel")).toHaveTextContent("artifact:none");
  });

  it("renders as a labelled complementary landmark", () => {
    render(
      <DraftInspector
        draftId={1}
        revisionId={1}
        jobId={1}
        stage={null}
        lockVersion={1}
        canDispose={false}
        tier="standard"
      />
    );
    expect(screen.getByRole("complementary", { name: "Draft inspector" })).toBeInTheDocument();
  });
});
