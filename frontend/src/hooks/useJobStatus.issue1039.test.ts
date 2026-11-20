// frontend/src/hooks/useJobStatus.issue1039.test.ts
//
// Issue #1039 ([Workstream M] follow-up to #784): the Activity tray's poll
// must never waste requests on the reindex family for principals that can
// never see it — GET /documents/reindex/jobs sits behind require_admin_role,
// so a member/viewer session's every tick was a guaranteed,
// swallowed-but-browser-logged 403 (~7.5 wasted requests/minute per mounted
// tray, 2026-10-28 deployment evidence).
//
// SERVER-ENFORCED SINCE #1157: the contract is now carried by the aggregate
// endpoint (GET /api/activity/jobs), which scopes reindex rows to admins the
// same way it scopes every family to the caller's readable vaults. The old
// client-side layers — the role-guess gate and the 403-learning backoff
// (including the PRR-001/PRR-003 review repairs) — are DELETED BY DESIGN: a
// member simply receives no reindex rows in the aggregate, so there is no
// request to skip and no 403 to learn from. These tests pin the aggregate
// seam from both sides of that scoping:
//   - the poller NEVER dispatches any legacy family route (reindex included)
//     across ticks — the waste class #1039 documented cannot recur, because
//     the only request the tray makes is getActivityJobs;
//   - a member-shaped response (no reindex rows) renders no reindex rows
//     while sibling families keep rendering (the AC1/AC3 guarantee);
//   - an admin-shaped response renders the reindex row with the server's
//     title (the AC2 guarantee);
//   - the response shape — not the client role snapshot — decides
//     visibility: the principal channel can no longer influence the poll,
//     which is the #1039 invariant restated for the server-enforced world;
//   - an aggregate 403 keeps the chain polling at cadence: the per-family
//     backoff layer is gone, and a single failed aggregate tick is a
//     one-tick degradation, never a learned skip.
//
// The principal is still driven through the REAL auth-lifecycle publication
// (publishAuthPrincipal) in the role-invariance test so the file keeps its
// linkage to the exact channel the old client gate read. Mock layout
// mirrors useJobStatus.audit1115.budget.test.tsx (hoisted factories; the
// vault-store stand-in stays mocked so its non-consultation stays asserted).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { publishAuthPrincipal } from "@/lib/api/auth-lifecycle";
import type { ActivityJob } from "@/lib/api";

const {
  mockGetActivityJobs,
  mockListDocuments,
  mockListReindexJobs,
  mockListWikiJobs,
  mockListKMSJobs,
  mockListDrafts,
  mockListDraftJobs,
  mockVaultState,
} = vi.hoisted(() => ({
  mockGetActivityJobs: vi.fn(),
  mockListDocuments: vi.fn(),
  mockListReindexJobs: vi.fn(),
  mockListWikiJobs: vi.fn(),
  mockListKMSJobs: vi.fn(),
  mockListDrafts: vi.fn(),
  mockListDraftJobs: vi.fn(),
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

/** A complete ActivityJob row; per-family fields default to their nulls. */
function activityJob(
  overrides: Partial<ActivityJob> & Pick<ActivityJob, "family" | "id">
): ActivityJob {
  return {
    vault_id: null,
    status: "running",
    title: "t",
    created_at: "2026-01-01T00:00:00Z",
    completed_at: null,
    draft_id: null,
    file_id: null,
    active_stage: null,
    phase: null,
    phase_message: null,
    progress_percent: null,
    processed_units: null,
    total_units: null,
    unit_label: null,
    error: null,
    ...overrides,
  };
}

/** The wire shape GET /api/activity/jobs answers with. */
function activityResponse(
  jobs: ActivityJob[] = [],
  changeToken = "t0",
  changed = false,
) {
  return { jobs, change_token: changeToken, changed };
}

/** Flush the synchronous mount tick's promise chain (see the m04 floor test). */
async function flushFirstTick() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** Advance the floored 1s poll chain across `ms` and settle its fetches. */
async function advanceTicks(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  mockVaultState.mockReturnValue({
    vaults: [{ id: 1 }],
    loading: false,
    fetchVaults: vi.fn().mockResolvedValue(undefined),
  });
  mockGetActivityJobs.mockReset();
  mockGetActivityJobs.mockResolvedValue(activityResponse());
  mockListDocuments.mockReset();
  mockListDocuments.mockResolvedValue({ documents: [], total: 0 });
  mockListReindexJobs.mockReset();
  mockListReindexJobs.mockResolvedValue({ jobs: [] });
  mockListWikiJobs.mockReset();
  mockListWikiJobs.mockResolvedValue({ jobs: [] });
  mockListKMSJobs.mockReset();
  mockListKMSJobs.mockResolvedValue({ jobs: [] });
  mockListDrafts.mockReset();
  mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
  mockListDraftJobs.mockReset();
  mockListDraftJobs.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 });
});

afterEach(() => {
  vi.useRealTimers();
  publishAuthPrincipal(null);
});

describe("useActivityJobs reindex visibility (issues #1039 / #1157 — server-enforced)", () => {
  it("AC1 (server-enforced): across ticks the poller dispatches ZERO legacy family routes — getActivityJobs is the only request", async () => {
    vi.useFakeTimers();

    renderHook(() => useActivityJobs({ intervalMs: 1000 }));

    await flushFirstTick();
    await advanceTicks(2300); // mount tick + ~2 scheduled ticks

    // The aggregate endpoint is alive (>= 3 ticks dispatched)...
    expect(mockGetActivityJobs.mock.calls.length).toBeGreaterThanOrEqual(3);
    // ...and none of the legacy family routes — reindex included, the exact
    // waste class #1039 documented — is ever consulted. The vault store is
    // not consulted either: scoping is the server's job now.
    expect(mockListReindexJobs).not.toHaveBeenCalled();
    expect(mockListDocuments).not.toHaveBeenCalled();
    expect(mockListWikiJobs).not.toHaveBeenCalled();
    expect(mockListKMSJobs).not.toHaveBeenCalled();
    expect(mockListDrafts).not.toHaveBeenCalled();
    expect(mockListDraftJobs).not.toHaveBeenCalled();
    expect(mockVaultState).not.toHaveBeenCalled();
  });

  it("AC1/AC3: a member-shaped response (no reindex rows) renders no reindex rows while sibling families keep rendering", async () => {
    // The server scopes reindex to admins: a member's aggregate simply has
    // no reindex rows, and the tray must neither invent nor miss anything.
    mockGetActivityJobs.mockResolvedValue(
      activityResponse(
        [
          activityJob({ family: "wiki", id: 31, vault_id: 1, status: "running", title: "Wiki compile" }),
          activityJob({ family: "kms", id: 11, vault_id: 1, status: "running", title: "KMS recompile" }),
        ],
        "t1",
        true
      )
    );

    const { result } = renderHook(() => useActivityJobs({ intervalMs: 8000 }));

    await waitFor(
      () => {
        expect(result.current.rows).toHaveLength(2);
      },
      { timeout: 8_000 }
    );
    // Sibling families RENDER (not just poll) — the AC3 half of the guarantee.
    expect(result.current.rows.map((r) => r.family).sort()).toEqual(["kms", "wiki"]);
    expect(result.current.rows.some((r) => r.family === "reindex")).toBe(false);
  });

  it("AC2: an admin-shaped response renders the reindex row with the server's title", async () => {
    mockGetActivityJobs.mockResolvedValue(
      activityResponse(
        [
          // vault_id is null only for admin-visible global reindex rows.
          activityJob({ family: "reindex", id: 21, vault_id: null, status: "running", title: "Embedding reindex (vault 4)" }),
        ],
        "t1",
        true
      )
    );

    const { result } = renderHook(() => useActivityJobs({ intervalMs: 8000 }));

    await waitFor(
      () => {
        expect(result.current.rows).toHaveLength(1);
      },
      { timeout: 8_000 }
    );
    const reindex = result.current.rows.filter((r) => r.family === "reindex");
    expect(reindex.map((r) => r.key)).toEqual(["reindex-21"]);
    expect(reindex[0]?.title).toBe("Embedding reindex (vault 4)");
    expect(reindex[0]?.phase).toBe("running");
    expect(reindex[0]?.terminal).toBe(false);
    expect(reindex[0]?.cancellable).toBe(false);
  });

  it("the response shape — not the client role snapshot — decides reindex visibility (the client-side gate is gone)", async () => {
    // Pre-#1157 the hook read the auth-lifecycle principal to guess whether
    // the reindex lane should dispatch. That gate is deleted: the SAME
    // publications must not change what renders. Drive the real channel
    // both ways and assert only the response decides.
    publishAuthPrincipal({ id: 501, role: "member" });
    mockGetActivityJobs.mockResolvedValue(
      activityResponse(
        [activityJob({ family: "reindex", id: 22, vault_id: null, status: "running", title: "Embedding reindex" })],
        "t1",
        true
      )
    );
    const memberView = renderHook(() => useActivityJobs({ intervalMs: 8000 }));
    await waitFor(
      () => {
        expect(memberView.result.current.rows.some((r) => r.family === "reindex")).toBe(true);
      },
      { timeout: 8_000 }
    );
    memberView.unmount();

    // Same member publication, member-shaped response: no reindex rows —
    // the client role snapshot neither adds nor hides anything.
    mockGetActivityJobs.mockResolvedValue(
      activityResponse(
        [activityJob({ family: "wiki", id: 31, vault_id: 1, status: "running", title: "Wiki compile" })],
        "t2",
        true
      )
    );
    const shapedView = renderHook(() => useActivityJobs({ intervalMs: 8000 }));
    await waitFor(
      () => {
        expect(shapedView.result.current.rows).toHaveLength(1);
      },
      { timeout: 8_000 }
    );
    expect(shapedView.result.current.rows.some((r) => r.family === "reindex")).toBe(false);

    // And a superadmin snapshot changes nothing either: the response is the
    // only input.
    publishAuthPrincipal({ id: 502, role: "superadmin" });
    const adminView = renderHook(() => useActivityJobs({ intervalMs: 8000 }));
    await waitFor(
      () => {
        expect(adminView.result.current.rows).toHaveLength(1);
      },
      { timeout: 8_000 }
    );
    expect(adminView.result.current.rows.some((r) => r.family === "reindex")).toBe(false);
  });

  it("a 403 from the aggregate keeps the chain polling at cadence (the 403-learning layer is gone)", async () => {
    vi.useFakeTimers();
    // Old world: the reindex lane's 403 armed a per-principal backoff and
    // the family was skipped. New world: the server scopes BEFORE a 403 can
    // exist, so an aggregate failure (any status) is a one-tick degradation
    // — the chain must keep its cadence, never learn to stop.
    mockGetActivityJobs.mockRejectedValue(
      Object.assign(new Error("Admin access required"), { status: 403 })
    );

    renderHook(() => useActivityJobs({ intervalMs: 1000 }));

    await flushFirstTick();
    expect(mockGetActivityJobs.mock.calls.length).toBe(1);

    await advanceTicks(2300);
    expect(mockGetActivityJobs.mock.calls.length).toBeGreaterThanOrEqual(3);
  });
});
