// frontend/src/hooks/useJobStatus.audit1115.budget.test.tsx
//
// Arm's-length acceptance check for the activity-API trace (audit
// 1115-1157-1158): the tray poller must poll ONE endpoint — a lib/api
// function `getActivityJobs` — instead of the five-family fan-out, with
// 8000ms live cadence, 32000ms idle backoff, and the existing hidden-tab
// skip preserved. RED at base via two independent legs:
//   (iii) the OLD per-family calls (listDocuments / listWikiJobs /
//         listKMSJobs / listDrafts+listDraftJobs) still fire every tick;
//   (mapping) a live row from getActivityJobs never reaches rows.
// (A bare "fewer than ten calls" count would be VACUOUSLY red at base — the
// hook never calls getActivityJobs today, so 0 < 10 holds — hence the
// aliveness leg (>= 1 call) and the zero-fan-out leg above.)
//
// Mock layout mirrors useJobStatus.issue1039.test.ts (hoisted factories, the
// vault-store stand-in, fake-timers flushFirstTick/advanceTicks, principal
// publication through auth-lifecycle). "@/lib/api" is mocked with the
// importActual spread PLUS getActivityJobs so the future single-endpoint
// poller finds its client while every old per-family module stays mocked to
// zero-tolerated call sites.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { publishAuthPrincipal } from "@/lib/api/auth-lifecycle";

const {
  mockGetActivityJobs,
  mockListDocuments,
  mockListWikiJobs,
  mockListKMSJobs,
  mockListDrafts,
  mockListDraftJobs,
  mockListReindexJobs,
  mockVaultState,
} = vi.hoisted(() => ({
  mockGetActivityJobs: vi.fn(),
  mockListDocuments: vi.fn(),
  mockListWikiJobs: vi.fn(),
  mockListKMSJobs: vi.fn(),
  mockListDrafts: vi.fn(),
  mockListDraftJobs: vi.fn(),
  mockListReindexJobs: vi.fn(),
  mockVaultState: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  getActivityJobs: mockGetActivityJobs,
}));

vi.mock("@/lib/api/core", () => ({
  listDocuments: mockListDocuments,
  listReindexJobs: mockListReindexJobs,
}));

vi.mock("@/lib/api/wiki", () => ({
  listWikiJobs: mockListWikiJobs,
}));

vi.mock("@/lib/api/kms", () => ({
  listKMSJobs: mockListKMSJobs,
}));

vi.mock("@/lib/api/draftRoom", () => ({
  listDrafts: mockListDrafts,
  listDraftJobs: mockListDraftJobs,
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: Object.assign(() => ({}), { getState: mockVaultState }),
}));

import { useActivityJobs } from "./useJobStatus";

/** The wire shape GET /api/activity/jobs answers with. */
function activityResponse(
  jobs: Array<Record<string, unknown>> = [],
  changeToken = "t0",
  changed = false,
) {
  return { jobs, change_token: changeToken, changed };
}

function liveWikiJob() {
  return {
    family: "wiki",
    id: 1,
    vault_id: 1,
    status: "running",
    title: "w",
    created_at: "2026-11-01T00:00:00Z",
    completed_at: null,
  };
}

/** Flush the synchronous mount tick's promise chain (issue1039 idiom). */
async function flushFirstTick() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** Advance the settle-driven poll chain across `ms` (issue1039 idiom). */
async function advanceTicks(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    value: hidden,
    configurable: true,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

function resetMocks() {
  mockVaultState.mockReturnValue({
    vaults: [{ id: 1 }],
    loading: false,
    fetchVaults: vi.fn().mockResolvedValue(undefined),
  });
  mockGetActivityJobs.mockReset();
  mockGetActivityJobs.mockResolvedValue(activityResponse());
  mockListDocuments.mockReset();
  mockListDocuments.mockResolvedValue({ documents: [], total: 0 });
  mockListWikiJobs.mockReset();
  mockListWikiJobs.mockResolvedValue({ jobs: [] });
  mockListKMSJobs.mockReset();
  mockListKMSJobs.mockResolvedValue({ jobs: [] });
  mockListDrafts.mockReset();
  mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
  mockListDraftJobs.mockReset();
  mockListDraftJobs.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 });
  mockListReindexJobs.mockReset();
  mockListReindexJobs.mockResolvedValue({ jobs: [] });
}

beforeEach(() => {
  vi.useFakeTimers();
  setHidden(false);
  // Sentinel-then-null: role unknown AND any armed backoff cleared.
  publishAuthPrincipal({ id: 111_500, role: "member" });
  publishAuthPrincipal(null);
  resetMocks();
});

afterEach(() => {
  vi.useRealTimers();
  publishAuthPrincipal(null);
});

describe("useActivityJobs single-endpoint poll budget (audit 1115)", () => {
  it("idle: one endpoint, alive, inside the 60s request budget, with zero per-family fan-out", async () => {
    publishAuthPrincipal({ id: 601, role: "member" });

    // NO intervalMs override: the default cadences themselves are under
    // test — 8000ms while live rows exist, 32000ms once the snapshot is
    // all-terminal (a shorter override would mask an idle-backoff
    // regression, exactly the M6 probe's kill target).
    renderHook(() => useActivityJobs());
    await flushFirstTick();

    // (ii) The poller is alive: the single endpoint is actually being asked
    // (an unwired hook would trivially satisfy any "fewer than N" budget).
    const callsAfterMount = mockGetActivityJobs.mock.calls.length;
    expect(callsAfterMount).toBeGreaterThanOrEqual(1);

    // (i) Idle budget: with zero live rows in every response, an entire
    // 60s window stays under ten requests (32000ms idle backoff cadence —
    // mounting included, at most mount + 2 idle ticks land inside 60s).
    await advanceTicks(60_000);
    const callsAfter60s = mockGetActivityJobs.mock.calls.length;
    expect(callsAfter60s - callsAfterMount).toBeLessThan(10);
    // And specifically no faster than the idle cadence: fewer than four
    // idle ticks in 60s rules out a collapsed/ignored backoff constant.
    expect(callsAfter60s - callsAfterMount).toBeLessThan(4);

    // (iii) Zero of the OLD per-family calls during that window — at base
    // the five-family fan-out fires on every tick, which is the waste the
    // single-endpoint poller replaces.
    expect(mockListDocuments).not.toHaveBeenCalled();
    expect(mockListWikiJobs).not.toHaveBeenCalled();
    expect(mockListKMSJobs).not.toHaveBeenCalled();
    expect(mockListDrafts).not.toHaveBeenCalled();
    expect(mockListDraftJobs).not.toHaveBeenCalled();
    expect(mockListReindexJobs).not.toHaveBeenCalled();
  });

  it("hidden: polling stops entirely and resumes when visible again", async () => {
    publishAuthPrincipal({ id: 602, role: "member" });

    renderHook(() => useActivityJobs());
    await flushFirstTick();
    const callsBeforeHidden = mockGetActivityJobs.mock.calls.length;

    setHidden(true);
    await advanceTicks(60_000);
    // The existing hidden-tab skip (useJobStatus.ts) must hold for the new
    // endpoint too: no requests while the document is hidden.
    expect(mockGetActivityJobs.mock.calls.length).toBe(callsBeforeHidden);

    setHidden(false);
    await advanceTicks(9_000);
    expect(mockGetActivityJobs.mock.calls.length).toBeGreaterThan(
      callsBeforeHidden,
    );
  });

  it("live: 8000ms cadence while a non-terminal row exists, mapped onto rows", async () => {
    publishAuthPrincipal({ id: 603, role: "member" });
    mockGetActivityJobs.mockResolvedValue(
      activityResponse([liveWikiJob()], "t1", true),
    );

    const { result } = renderHook(() => useActivityJobs());
    await flushFirstTick();

    // Row mapping: the live wiki job arrives as a non-terminal wiki row.
    const wikiRows = result.current.rows.filter((r) => r.family === "wiki");
    expect(wikiRows).toHaveLength(1);
    expect(wikiRows[0].terminal).toBe(false);
    expect(wikiRows[0].status).toBe("running");

    // Cadence: while a live row exists the poller stays at 8000ms — at
    // least 3 further dispatches inside a 32s window.
    const callsAtStart = mockGetActivityJobs.mock.calls.length;
    await advanceTicks(32_000);
    expect(mockGetActivityJobs.mock.calls.length - callsAtStart).toBeGreaterThanOrEqual(3);
  });
});
