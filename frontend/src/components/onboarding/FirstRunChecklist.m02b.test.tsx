// frontend/src/components/onboarding/FirstRunChecklist.m02b.test.tsx
// Issue-trace 782-firstrun-checklist-vaultgate-kms-wiki (unfrozen companion
// to frozen C6): pins the fail-open shell contract the frozen check cannot
// see — null while pending, null on fetch error, null when hidden, no live
// region while shown, dismissal hides locally, the poll refetches while
// shown, and the interval stops after unmount.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";

vi.mock("@/lib/api/onboarding", () => ({
  getOnboardingMilestones: vi.fn(),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

import FirstRunChecklist from "@/components/onboarding/FirstRunChecklist";
import {
  dismissChecklist,
  getOnboardingMilestones,
} from "@/lib/api/onboarding";

const COMPLETE = {
  vault_created: true,
  upload_indexed: false,
  first_question_asked: true,
  first_citation_opened: false,
  show_checklist: true,
};

describe("FirstRunChecklist m02b (shell fail-open contract, issue #782)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(dismissChecklist).mockResolvedValue(undefined);
  });

  it("renders nothing while the first fetch is pending", () => {
    vi.mocked(getOnboardingMilestones).mockReturnValue(
      new Promise(() => {}),
    );
    const { container } = render(<FirstRunChecklist />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing on fetch error (fail-open)", async () => {
    vi.mocked(getOnboardingMilestones).mockRejectedValue(
      new Error("milestones endpoint down"),
    );
    const { container } = render(<FirstRunChecklist />);
    await waitFor(() => {
      expect(getOnboardingMilestones).toHaveBeenCalled();
    });
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when the server says show_checklist is false", async () => {
    vi.mocked(getOnboardingMilestones).mockResolvedValue({
      ...COMPLETE,
      show_checklist: false,
    });
    const { container } = render(<FirstRunChecklist />);
    await waitFor(() => {
      expect(getOnboardingMilestones).toHaveBeenCalled();
    });
    expect(container.firstChild).toBeNull();
  });

  it("uses no live region (role=status / aria-live) while shown", async () => {
    vi.mocked(getOnboardingMilestones).mockResolvedValue(COMPLETE);
    render(<FirstRunChecklist />);
    await waitFor(() => {
      expect(
        screen.getByTestId("first-run-checklist"),
      ).toBeInTheDocument();
    });
    // PageShell-level tests assert no second live region appears; the
    // checklist is static guidance, not an async status update.
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(
      document.querySelector("[aria-live]"),
    ).toBeNull();
  });

  it("hides on dismiss and records the dismissal server-side", async () => {
    vi.mocked(getOnboardingMilestones).mockResolvedValue(COMPLETE);
    const user = userEvent.setup();
    render(<FirstRunChecklist />);
    await waitFor(() => {
      expect(
        screen.getByTestId("first-run-checklist"),
      ).toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(
      screen.queryByTestId("first-run-checklist"),
    ).not.toBeInTheDocument();
    expect(dismissChecklist).toHaveBeenCalledTimes(1);
  });

  describe("polling", () => {
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it("refetches while shown so milestone flips appear without a reload", async () => {
      // Call 1: initial mount load (checklist hidden state unknown).
      // Call 2: the visible-flip effect rerun (checklist now shown).
      // Call 3+: the 3s poll — this is the flip the test proves.
      vi.mocked(getOnboardingMilestones)
        .mockResolvedValueOnce(COMPLETE)
        .mockResolvedValueOnce(COMPLETE)
        .mockResolvedValue({ ...COMPLETE, upload_indexed: true });
      const { container } = render(<FirstRunChecklist />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByTestId("first-run-checklist")).toBeInTheDocument();
      expect(
        screen.getAllByTestId("checklist-milestone")[1].getAttribute("data-done"),
      ).toBe("false");

      await act(async () => {
        await vi.advanceTimersByTimeAsync(3500);
      });
      expect(
        screen.getAllByTestId("checklist-milestone")[1].getAttribute("data-done"),
      ).toBe("true");
      expect(container.firstChild).not.toBeNull();
    });

    it("stops refetching after unmount (interval cleared)", async () => {
      vi.mocked(getOnboardingMilestones).mockResolvedValue(COMPLETE);
      const { unmount } = render(<FirstRunChecklist />);
      await waitFor(() => {
        expect(screen.getByTestId("first-run-checklist")).toBeInTheDocument();
      });
      unmount();
      const callsAtUnmount = vi.mocked(getOnboardingMilestones).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10000);
      });
      expect(vi.mocked(getOnboardingMilestones).mock.calls.length).toBe(
        callsAtUnmount,
      );
    });
  });
});
