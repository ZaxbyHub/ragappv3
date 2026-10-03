// frontend/src/components/onboarding/FirstRunChecklist.tsx
// Issue #782 (UI-ENH-07 stage 2): dismissible first-run checklist whose four
// milestones (vault created, upload indexed, first question asked, first
// citation opened) come from the SERVER via GET /api/onboarding/milestones —
// never reconstructed client-side (audit review correction C4).
//
// Mounts inside PageShell's <main>, so it is on screen wherever the user
// lands after the Setup wizard finishes (navigate("/") -> /documents ->
// shell) without interrupting the wizard itself.
//
// Contract notes (frozen by trace 782 acceptance check C6 and its unfrozen
// m02b companion):
// - self-fetching useEffect + useState: the component renders bare, with no
//   QueryClient and no Router, so no react-query / router hooks are allowed;
// - renders null while the first fetch is pending, on fetch failure (a
//   broken milestones endpoint must never break the shell), and whenever the
//   server says show_checklist is false;
// - deliberately NO role="status"/aria-live wrapper: PageShell-level tests
//   assert that no second live region appears, and the checklist is not an
//   asynchronous status update;
// - one element with data-testid="checklist-milestone" per milestone, each
//   carrying data-done="true"|"false" so done/not-done is discriminated.

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  dismissChecklist,
  getOnboardingMilestones,
  type OnboardingMilestones,
} from "@/lib/api/onboarding";

const POLL_INTERVAL_MS = 3000;

const MILESTONES: ReadonlyArray<{
  key: keyof Omit<OnboardingMilestones, "show_checklist">;
  label: string;
}> = [
  { key: "vault_created", label: "Create a vault" },
  { key: "upload_indexed", label: "Index a document" },
  { key: "first_question_asked", label: "Ask a question" },
  { key: "first_citation_opened", label: "Open a citation" },
];

export default function FirstRunChecklist() {
  const [milestones, setMilestones] = useState<OnboardingMilestones | null>(
    null,
  );
  const [dismissed, setDismissed] = useState(false);

  const visible =
    milestones !== null && milestones.show_checklist && !dismissed;

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const next = await getOnboardingMilestones();
        if (!cancelled) {
          setMilestones(next);
        }
      } catch {
        // Fail-open: onboarding hints never break the shell.
      }
    };
    void load();
    if (!visible) {
      return () => {
        cancelled = true;
      };
    }
    // Poll only while the checklist is shown, so completed/dismissed users
    // cost a single request per shell mount.
    const id = window.setInterval(() => {
      void load();
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [visible]);

  if (!visible) {
    return null;
  }

  const onDismiss = (event: React.MouseEvent<HTMLButtonElement>) => {
    // Move focus off the button before it unmounts (a11y: a removed focused
    // control drops focus to <body>) — same shape as UnconfiguredChatBanner.
    const main = event.currentTarget.ownerDocument.getElementById(
      "main-content",
    );
    main?.focus();
    setDismissed(true);
    // Server-side dismissal persists across sessions; a failed write just
    // means the checklist reappears on the next load.
    void dismissChecklist().catch(() => undefined);
  };

  return (
    <div
      data-testid="first-run-checklist"
      className="border-b border-border bg-muted/40 px-4 py-3"
    >
      <div className="mx-auto flex max-w-3xl flex-col gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-medium text-foreground">
            Getting started
          </h2>
          <p className="flex-1 text-xs text-muted-foreground">
            Four steps to your first cited answer.
          </p>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onDismiss}
            aria-label="Dismiss"
          >
            <X className="h-4 w-4" aria-hidden="true" />
            Dismiss
          </Button>
        </div>
        <ul className="flex flex-wrap items-center gap-x-6 gap-y-1">
          {MILESTONES.map((milestone) => {
            const done = milestones[milestone.key];
            return (
              <li
                key={milestone.key}
                data-testid="checklist-milestone"
                data-done={String(done)}
                className={
                  done
                    ? "flex items-center gap-1.5 text-xs text-muted-foreground"
                    : "flex items-center gap-1.5 text-xs font-medium text-foreground"
                }
              >
                <span
                  aria-hidden="true"
                  className={
                    done
                      ? "inline-block h-2 w-2 rounded-full bg-emerald-500"
                      : "inline-block h-2 w-2 rounded-full border border-muted-foreground/60"
                  }
                />
                {milestone.label}
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
