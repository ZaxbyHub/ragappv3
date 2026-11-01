// frontend/src/components/onboarding/FirstRunChecklist.m02.test.tsx
// Issue-trace 782-firstrun-checklist-vaultgate-kms-wiki — acceptance check C6 (AC6).
//
// NEW-SURFACE check: neither the component module
// (@/components/onboarding/FirstRunChecklist — the path is the contract) nor
// the milestone client module (@/lib/api/onboarding) exists at base, so this
// file fails during module resolution ("Failed to resolve import"), which is
// the pre-fix RED signature.
//
// Post-fix contract under test — the component renders SERVER milestone
// state (fetched via getOnboardingMilestones), not a hardcoded client-side
// checklist:
//   - one element with data-testid="checklist-milestone" per milestone
//     (4 milestones: vault_created, upload_indexed, first_question_asked,
//     first_citation_opened);
//   - each such element carries data-done="true"|"false" discriminating
//     done vs not-done;
//   - a dismiss control reachable by accessible name /dismiss/i exists.
//
// The client-module contract mocked here: @/lib/api/onboarding exports
// getOnboardingMilestones(): Promise<OnboardingMilestones> (the GET
// /api/onboarding/milestones payload: the four milestone flags plus
// show_checklist), markCitationOpened(): Promise<void> (the
// citation-opened write), and dismissChecklist(): Promise<void> (the
// dismiss write).

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

vi.mock("@/lib/api/onboarding", () => ({
  getOnboardingMilestones: vi.fn(),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

import FirstRunChecklist from "@/components/onboarding/FirstRunChecklist";
import { getOnboardingMilestones } from "@/lib/api/onboarding";

describe("FirstRunChecklist m02 (issue-trace 782-firstrun-checklist-vaultgate-kms-wiki)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 2-of-4 complete server state: vault created and a question asked,
    // nothing indexed yet, no citation opened.
    vi.mocked(getOnboardingMilestones).mockResolvedValue({
      vault_created: true,
      upload_indexed: false,
      first_question_asked: true,
      first_citation_opened: false,
      show_checklist: true,
    });
  });

  it("renders server milestone state", async () => {
    await act(async () => {
      render(<FirstRunChecklist />);
    });

    // Four milestone items render from the server payload.
    await waitFor(() => {
      expect(screen.queryAllByTestId("checklist-milestone").length).toBe(4);
    });

    // Exactly two are done and two are not — the done/not-done marking must
    // discriminate (data-done="true" vs "false").
    const items = screen.queryAllByTestId("checklist-milestone");
    const done = items.filter((el) => el.getAttribute("data-done") === "true");
    const notDone = items.filter((el) => el.getAttribute("data-done") === "false");
    expect(done.length).toBe(2);
    expect(notDone.length).toBe(2);

    // A dismiss control is reachable by accessible name.
    expect(screen.getByRole("button", { name: /dismiss/i })).toBeInTheDocument();
  });
});
