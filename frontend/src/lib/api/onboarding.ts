// frontend/src/lib/api/onboarding.ts
// Issue #782 (UI-ENH-07 stage 2): first-run checklist milestone client.
//
// Contract (frozen by trace 782 acceptance checks C3-C6): the GET payload is
// the five top-level booleans; the two writes are POST-only and csrf-protected
// by the apiClient interceptor. Deliberately NOT re-exported through the
// `@/lib/api` barrel — existing suites mock that barrel with partial surfaces
// (e.g. PageShell.test.tsx mocks only getSettings), so every consumer imports
// this module directly.

import { apiClient } from "./core";

export interface OnboardingMilestones {
  vault_created: boolean;
  upload_indexed: boolean;
  first_question_asked: boolean;
  first_citation_opened: boolean;
  show_checklist: boolean;
}

export async function getOnboardingMilestones(): Promise<OnboardingMilestones> {
  const response = await apiClient.get<OnboardingMilestones>(
    "/onboarding/milestones",
  );
  return response.data;
}

export async function markCitationOpened(): Promise<void> {
  await apiClient.post("/onboarding/milestones/citation-opened");
}

export async function dismissChecklist(): Promise<void> {
  await apiClient.post("/onboarding/milestones/dismiss");
}

// The citation-opened milestone is once-per-user server-side (the upsert keeps
// the first timestamp); this client-side guard stops the chat surfaces from
// re-POSTing on every source click after the first acknowledged report. It
// latches ONLY on a 2xx — a failed report retries on the next click.
let citationReportAcknowledged = false;

/** Report "user opened a citation" once per page-life (fire-and-forget safe). */
export async function reportCitationOpened(): Promise<void> {
  if (citationReportAcknowledged) {
    return;
  }
  try {
    await markCitationOpened();
    citationReportAcknowledged = true;
  } catch {
    // Fire-and-forget telemetry for an onboarding hint: a failed report must
    // never break the citation-opening interaction.
  }
}

/**
 * Clear the once-guard so the next citation open reports again. Called on
 * logout: the guard is module-scoped and the SPA's logout does not reload the
 * page, so without this a second user on the same tab would inherit the first
 * user's latched milestone (issue #849 review EXT-001).
 */
export function resetCitationReport(): void {
  citationReportAcknowledged = false;
}
