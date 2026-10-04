// frontend/src/lib/api/onboarding.m02.test.ts
// Issue-trace 782-firstrun-checklist-vaultgate-kms-wiki (unfrozen pin):
// the milestone client's paths/methods and the reportCitationOpened
// once-guard semantics (latches ONLY on 2xx — a failed report retries on
// the next click; an acknowledged one never re-POSTs).
//
// The once-guard is module-scoped for the page-life, so this file runs the
// guard test exactly once (test order below is the only consumer).

import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("./core", () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

import { apiClient } from "./core";
import {
  getOnboardingMilestones,
  markCitationOpened,
  dismissChecklist,
  reportCitationOpened,
  resetCitationReport,
} from "./onboarding";

const mockedGet = vi.mocked(apiClient.get);
const mockedPost = vi.mocked(apiClient.post);

describe("lib/api/onboarding m02 (issue #782)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("GETs /onboarding/milestones and returns the five flags", async () => {
    const payload = {
      vault_created: true,
      upload_indexed: false,
      first_question_asked: false,
      first_citation_opened: false,
      show_checklist: true,
    };
    mockedGet.mockResolvedValue({ data: payload });
    await expect(getOnboardingMilestones()).resolves.toEqual(payload);
    expect(mockedGet).toHaveBeenCalledWith("/onboarding/milestones");
  });

  it("POSTs the two writes to their contract paths", async () => {
    mockedPost.mockResolvedValue({ data: { ok: true } });
    await markCitationOpened();
    await dismissChecklist();
    expect(mockedPost).toHaveBeenNthCalledWith(
      1,
      "/onboarding/milestones/citation-opened",
    );
    expect(mockedPost).toHaveBeenNthCalledWith(
      2,
      "/onboarding/milestones/dismiss",
    );
  });

  it("reportCitationOpened retries after a failed report and stops after 2xx", async () => {
    // First report fails (transient 5xx): the guard must NOT latch.
    mockedPost.mockRejectedValueOnce(new Error("503"));
    // Subsequent reports succeed (2xx): latches.
    mockedPost.mockResolvedValue({ data: { ok: true } });
    await reportCitationOpened();
    expect(mockedPost).toHaveBeenCalledTimes(1);

    await reportCitationOpened();
    expect(mockedPost).toHaveBeenCalledTimes(2);

    // After the latch no further POST happens, even on later clicks.
    mockedPost.mockClear();
    mockedPost.mockRejectedValue(new Error("503"));
    await reportCitationOpened();
    expect(mockedPost).not.toHaveBeenCalled();
  });

  it("resetCitationReport clears the latch (logout wiring, EXT-001)", async () => {
    // The SPA's logout does not reload the page, so a same-tab user switch
    // relies on useAuthStore.logout calling resetCitationReport — pin the
    // reset semantics the logout wiring depends on. The guard is
    // module-scoped and the earlier test latched it; reset to a known state.
    resetCitationReport();
    mockedPost.mockResolvedValue({ data: { ok: true } });
    await reportCitationOpened();
    expect(mockedPost).toHaveBeenCalledTimes(1);
    await reportCitationOpened();
    expect(mockedPost).toHaveBeenCalledTimes(1); // still latched

    resetCitationReport();
    await reportCitationOpened();
    expect(mockedPost).toHaveBeenCalledTimes(2); // reports again
  });
});
