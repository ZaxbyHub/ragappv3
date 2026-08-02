import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DraftStatusBanner } from "./DraftStatusBanner";
import {
  DRAFT_COMPLETE_REVIEW_REQUIRED,
  DRAFT_NOT_FACT_CHECKED,
  EVIDENCE_INVALIDATED_WARNING,
  READY_BLOCKER_LABELS,
  RETRIEVAL_PARTIAL_WARNING,
  SOURCE_ONLY_WARNING,
} from "@/components/draft-room/labels";
import type { DraftSummary } from "@/lib/api/draftRoom";

function makeDraft(overrides: Partial<DraftSummary> = {}): DraftSummary {
  return {
    id: 1,
    vault_id: 1,
    vault_access: "write",
    title: "Test draft",
    mode: "rewrite",
    status: "draft",
    tier: "standard",
    lock_version: 1,
    current_revision_id: 1,
    active_job_id: null,
    input_count: 1,
    open_blocker_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ready_at: null,
    ...overrides,
  };
}

const HEX_OR_LITERAL_COLOR = /#[0-9a-f]{3,8}\b|text-white\b|bg-black\b/i;

describe("DraftStatusBanner", () => {
  it("renders nothing when no condition applies", () => {
    const { container } = render(
      <DraftStatusBanner
        draft={makeDraft()}
        factStatus="passed"
        sourceOnly={false}
        retrievalPartial={false}
        evidenceInvalidated={false}
        vaultAccess="write"
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the exact source-only warning", () => {
    render(
      <DraftStatusBanner
        draft={makeDraft()}
        factStatus="passed"
        sourceOnly
        retrievalPartial={false}
        evidenceInvalidated={false}
        vaultAccess="write"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(SOURCE_ONLY_WARNING);
  });

  it("renders the exact retrieval-partial warning", () => {
    render(
      <DraftStatusBanner
        draft={makeDraft()}
        factStatus="passed"
        sourceOnly={false}
        retrievalPartial
        evidenceInvalidated={false}
        vaultAccess="write"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(RETRIEVAL_PARTIAL_WARNING);
  });

  it("renders the exact evidence-invalidated warning", () => {
    render(
      <DraftStatusBanner
        draft={makeDraft()}
        factStatus="invalidated"
        sourceOnly={false}
        retrievalPartial={false}
        evidenceInvalidated
        vaultAccess="write"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(EVIDENCE_INVALIDATED_WARNING);
  });

  it("renders the exact fact-not-current copy", () => {
    render(
      <DraftStatusBanner
        draft={makeDraft()}
        factStatus="not_run"
        sourceOnly={false}
        retrievalPartial={false}
        evidenceInvalidated={false}
        vaultAccess="write"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(DRAFT_NOT_FACT_CHECKED);
  });

  it("renders the exact needs-review copy", () => {
    render(
      <DraftStatusBanner
        draft={makeDraft({ status: "needs_review" })}
        factStatus="passed"
        sourceOnly={false}
        retrievalPartial={false}
        evidenceInvalidated={false}
        vaultAccess="write"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(DRAFT_COMPLETE_REVIEW_REQUIRED);
  });

  it("renders the vault-revoked warning", () => {
    render(
      <DraftStatusBanner
        draft={makeDraft()}
        factStatus="passed"
        sourceOnly={false}
        retrievalPartial={false}
        evidenceInvalidated={false}
        vaultAccess="revoked"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(READY_BLOCKER_LABELS.vault_access_revoked);
  });

  it("renders the archived read-only notice", () => {
    render(
      <DraftStatusBanner
        draft={makeDraft({ status: "archived" })}
        factStatus="passed"
        sourceOnly={false}
        retrievalPartial={false}
        evidenceInvalidated={false}
        vaultAccess="read"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/archived/i);
    expect(screen.getByRole("alert")).toHaveTextContent(/read-only/i);
  });

  it("orders multiple simultaneous alerts by priority, highest first", () => {
    render(
      <DraftStatusBanner
        draft={makeDraft({ status: "needs_review" })}
        factStatus="not_run"
        sourceOnly
        retrievalPartial
        evidenceInvalidated
        vaultAccess="revoked"
      />,
    );
    const alerts = screen.getAllByRole("alert");
    expect(alerts).toHaveLength(6);
    expect(alerts[0]).toHaveTextContent(READY_BLOCKER_LABELS.vault_access_revoked);
    expect(alerts[1]).toHaveTextContent(EVIDENCE_INVALIDATED_WARNING);
    expect(alerts[2]).toHaveTextContent(RETRIEVAL_PARTIAL_WARNING);
    expect(alerts[3]).toHaveTextContent(SOURCE_ONLY_WARNING);
    expect(alerts[4]).toHaveTextContent(DRAFT_NOT_FACT_CHECKED);
    expect(alerts[5]).toHaveTextContent(DRAFT_COMPLETE_REVIEW_REQUIRED);
  });

  it("renders a rerun-newsroom CTA only when the callback is supplied, and fires it on click", async () => {
    const user = userEvent.setup();
    const onRerunNewsroom = vi.fn();
    const { rerender } = render(
      <DraftStatusBanner
        draft={makeDraft()}
        factStatus="invalidated"
        sourceOnly={false}
        retrievalPartial={false}
        evidenceInvalidated
        vaultAccess="write"
      />,
    );
    expect(screen.queryByRole("button", { name: /rerun newsroom/i })).not.toBeInTheDocument();

    rerender(
      <DraftStatusBanner
        draft={makeDraft()}
        factStatus="invalidated"
        sourceOnly={false}
        retrievalPartial={false}
        evidenceInvalidated
        vaultAccess="write"
        onRerunNewsroom={onRerunNewsroom}
      />,
    );
    const cta = screen.getByRole("button", { name: /rerun newsroom/i });
    await user.click(cta);
    expect(onRerunNewsroom).toHaveBeenCalledTimes(1);
  });

  it("renders a rerun-research CTA only when the callback is supplied, and fires it on click", async () => {
    const user = userEvent.setup();
    const onRerunResearch = vi.fn();
    render(
      <DraftStatusBanner
        draft={makeDraft()}
        factStatus="passed"
        sourceOnly={false}
        retrievalPartial
        evidenceInvalidated={false}
        vaultAccess="write"
        onRerunResearch={onRerunResearch}
      />,
    );
    const cta = screen.getByRole("button", { name: /rerun research/i });
    await user.click(cta);
    expect(onRerunResearch).toHaveBeenCalledTimes(1);
  });

  it("gives every alert an icon and role=alert", () => {
    render(
      <DraftStatusBanner
        draft={makeDraft()}
        factStatus="passed"
        sourceOnly
        retrievalPartial={false}
        evidenceInvalidated={false}
        vaultAccess="write"
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(alert.querySelector("svg")).toBeInTheDocument();
  });

  it("contains no hard-coded hex colours or text-white/bg-black literals", () => {
    const { container } = render(
      <DraftStatusBanner
        draft={makeDraft({ status: "needs_review" })}
        factStatus="not_run"
        sourceOnly
        retrievalPartial
        evidenceInvalidated
        vaultAccess="revoked"
        onRerunResearch={vi.fn()}
        onRerunNewsroom={vi.fn()}
      />,
    );
    expect(container.innerHTML).not.toMatch(HEX_OR_LITERAL_COLOR);
  });
});
