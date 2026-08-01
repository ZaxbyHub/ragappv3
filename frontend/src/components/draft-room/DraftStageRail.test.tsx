import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DraftStageRail } from "./DraftStageRail";
import type { DraftStage, DraftJobStatus } from "@/lib/api/draftRoom";

const STAGE_ORDER = ["research", "outline", "draft", "lint", "copy", "standards", "fact"];

function makeStage(overrides: Partial<DraftStage> & Pick<DraftStage, "stage" | "status">): DraftStage {
  return {
    id: 1,
    job_id: 1,
    attempt: 1,
    input_sha256: "sha",
    artifact_sha256: null,
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
    artifact: null,
    content_md: null,
    ...overrides,
  };
}

function renderRail(overrides?: {
  stages?: DraftStage[];
  activeStage?: string | null;
  selectedStage?: string | null;
  jobStatus?: DraftJobStatus | null;
  blockerCountsByStage?: Record<string, number>;
  warningCountsByStage?: Record<string, number>;
  onSelectStage?: (stage: string) => void;
}) {
  const onSelectStage = overrides?.onSelectStage ?? vi.fn();
  render(
    <DraftStageRail
      stageOrder={STAGE_ORDER}
      stages={overrides?.stages ?? []}
      activeStage={overrides?.activeStage ?? null}
      selectedStage={overrides?.selectedStage ?? null}
      onSelectStage={onSelectStage}
      jobStatus={overrides?.jobStatus ?? null}
      blockerCountsByStage={overrides?.blockerCountsByStage}
      warningCountsByStage={overrides?.warningCountsByStage}
    />,
  );
  return { onSelectStage };
}

describe("DraftStageRail", () => {
  it("renders every stage in stageOrder with the derived state", () => {
    const stages: DraftStage[] = [
      makeStage({ stage: "research", status: "completed" }),
      makeStage({ stage: "outline", status: "running" }),
      makeStage({ stage: "draft", status: "failed", error_code: "model_unavailable" }),
    ];
    renderRail({ stages, activeStage: "outline", jobStatus: "running" });

    expect(screen.getByRole("button", { name: /^Research, complete$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Outline, running$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Draft, failed, model_unavailable$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Lint, pending$/i })).toBeInTheDocument();
  });

  it("moves focus with arrow keys and jumps to the ends with Home/End", async () => {
    const user = userEvent.setup();
    renderRail();
    const buttons = screen.getAllByRole("button");

    buttons[0].focus();
    expect(buttons[0]).toHaveFocus();

    await user.keyboard("{ArrowRight}");
    expect(buttons[1]).toHaveFocus();

    await user.keyboard("{ArrowRight}");
    expect(buttons[2]).toHaveFocus();

    await user.keyboard("{ArrowLeft}");
    expect(buttons[1]).toHaveFocus();

    await user.keyboard("{End}");
    expect(buttons[buttons.length - 1]).toHaveFocus();

    await user.keyboard("{Home}");
    expect(buttons[0]).toHaveFocus();
  });

  it("keeps a single tab stop into the rail", () => {
    renderRail({ selectedStage: "outline" });
    const buttons = screen.getAllByRole("button");
    const tabbable = buttons.filter((button) => button.getAttribute("tabindex") === "0");
    expect(tabbable).toHaveLength(1);
  });

  it("includes the stage's state in the accessible name", () => {
    const stages: DraftStage[] = [makeStage({ stage: "research", status: "completed" })];
    renderRail({ stages });
    expect(screen.getByRole("button", { name: /research, complete/i })).toBeInTheDocument();
  });

  it("calls onSelectStage when a stage button is clicked", async () => {
    const user = userEvent.setup();
    const { onSelectStage } = renderRail();
    await user.click(screen.getByRole("button", { name: /^Research, pending$/i }));
    expect(onSelectStage).toHaveBeenCalledWith("research");
  });

  it("renders blocker and warning counts as visible text, not colour alone", () => {
    const stages: DraftStage[] = [
      makeStage({ stage: "fact", status: "completed" }),
      makeStage({ stage: "standards", status: "completed" }),
    ];
    renderRail({
      stages,
      blockerCountsByStage: { fact: 2 },
      warningCountsByStage: { standards: 1 },
    });

    expect(screen.getByRole("button", { name: /^Fact, blocked, 2 blockers$/i })).toBeInTheDocument();
    expect(screen.getByText("2 blockers")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Standards, warning, 1 warning$/i })).toBeInTheDocument();
    expect(screen.getByText("1 warning")).toBeInTheDocument();
  });

  it("surfaces the stable error code for a failed stage", () => {
    const stages: DraftStage[] = [makeStage({ stage: "draft", status: "failed", error_code: "job_timeout" })];
    renderRail({ stages });
    expect(screen.getByRole("button", { name: /^Draft, failed, job_timeout$/i })).toBeInTheDocument();
    expect(screen.getByText("job_timeout")).toBeInTheDocument();
  });

  it("renders all stages as pending without throwing when stages is empty", () => {
    expect(() => renderRail({ stages: [] })).not.toThrow();
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(STAGE_ORDER.length);
    for (const button of buttons) {
      expect(button).toHaveAccessibleName(/, pending$/i);
    }
  });
});
