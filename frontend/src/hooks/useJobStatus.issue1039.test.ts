// frontend/src/hooks/useJobStatus.issue1039.test.ts
//
// Issue #1039 ([Workstream M] follow-up to #784): the Activity tray's
// aggregation poll must NOT dispatch the reindex family for principals that
// can never see it — GET /documents/reindex/jobs sits behind
// require_admin_role, so a member/viewer session's every tick was a
// guaranteed, swallowed-but-browser-logged 403 (~7.5 wasted requests/minute
// per mounted tray, 2026-10-28 deployment evidence).
//
// Discriminating coverage (AC1): member/viewer principals mount the tray
// poller, advance multiple ticks, and dispatch ZERO reindex requests while
// the other four families keep polling and rendering (AC3). Preserving
// coverage (AC2): admin/superadmin principals keep the family every poll.
// Backoff coverage: a role-less principal (auth still hydrating, or a
// user-less session shape like single_admin) gets exactly one 403, then the
// family is skipped until the principal changes; non-403 failures never arm
// the backoff; a later admin publication overrides an armed backoff.
//
// The principal is driven through the REAL auth-lifecycle publication
// (publishAuthPrincipal) — the same channel useAuthStore feeds in the app —
// so these tests exercise the exact gate the production shell reads. Mock
// layout mirrors useJobStatus.m04.test.ts (hoisted factories; the vault
// store stand-in; the real documentProgress).
//
// Ordering note: the beforeEach sentinel publication both resets the role
// to null AND bumps the principal generation, which clears any backoff a
// previous test armed — the suite stays order-independent.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { publishAuthPrincipal } from "@/lib/api/auth-lifecycle";

const {
  mockListDocuments,
  mockListReindexJobs,
  mockListKMSJobs,
  mockListDrafts,
  mockListDraftJobs,
  mockVaultState,
} = vi.hoisted(() => ({
  mockListDocuments: vi.fn(),
  mockListReindexJobs: vi.fn(),
  mockListKMSJobs: vi.fn(),
  mockListDrafts: vi.fn(),
  mockListDraftJobs: vi.fn(),
  mockVaultState: vi.fn(),
}));

vi.mock("@/lib/api/core", () => ({
  listDocuments: mockListDocuments,
  listReindexJobs: mockListReindexJobs,
}));

vi.mock("@/lib/api/wiki", () => ({
  listWikiJobs: vi.fn(),
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
import { listWikiJobs } from "@/lib/api/wiki";

function wikiJob(id: number, status: string) {
  return {
    id,
    vault_id: 1,
    trigger_type: "manual",
    trigger_id: null,
    status,
    error: null,
    result_json: "{}",
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    completed_at: null,
    retry_count: 0,
  };
}

function reindexJob(id: number, status: string, vaultId: number | null = 4) {
  return {
    id,
    vault_id: vaultId,
    trigger_type: "api",
    trigger_id: "1",
    status,
    error: null,
    result_json: "{}",
    input_json: "{}",
    retry_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    completed_at: null,
  };
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

/** Publish the sentinel-then-null pair: role unknown AND backoff cleared. */
function resetPrincipalToUnknown() {
  publishAuthPrincipal({ id: 103_900, role: "member" });
  publishAuthPrincipal(null);
}

beforeEach(() => {
  resetPrincipalToUnknown();
  mockVaultState.mockReturnValue({
    vaults: [{ id: 1 }],
    loading: false,
    fetchVaults: vi.fn().mockResolvedValue(undefined),
  });
  mockListDocuments.mockReset();
  mockListDocuments.mockResolvedValue({ documents: [], total: 0 });
  mockListKMSJobs.mockReset();
  mockListKMSJobs.mockResolvedValue({ jobs: [] });
  mockListDrafts.mockReset();
  mockListDrafts.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 10 });
  mockListDraftJobs.mockReset();
  mockListDraftJobs.mockResolvedValue({ items: [], total: 0, page: 1, per_page: 20 });
  mockListReindexJobs.mockReset();
  mockListReindexJobs.mockResolvedValue({ jobs: [] });
  const wikiMock = vi.mocked(listWikiJobs);
  wikiMock.mockReset();
  wikiMock.mockResolvedValue({ jobs: [] });
});

afterEach(() => {
  vi.useRealTimers();
  publishAuthPrincipal(null);
});

describe("useActivityJobs reindex-family visibility (issue #1039)", () => {
  it.each(["member", "viewer"])(
    "AC1/AC3: a %s principal dispatches zero reindex requests while the other four families keep polling and rendering",
    async (role) => {
      vi.useFakeTimers();
      publishAuthPrincipal({ id: 501, role });
      // A live wiki row proves sibling families still RENDER (not just poll).
      const wikiMock = vi.mocked(listWikiJobs);
      wikiMock.mockResolvedValue({ jobs: [wikiJob(31, "running")] });

      const { result } = renderHook(() => useActivityJobs({ intervalMs: 1000 }));

      await flushFirstTick();
      await advanceTicks(2300); // mount tick + ~2 scheduled ticks

      expect(mockListReindexJobs).not.toHaveBeenCalled();
      // Every sibling family kept its per-tick cadence (>= 3 ticks).
      expect(wikiMock.mock.calls.length).toBeGreaterThanOrEqual(3);
      expect(mockListKMSJobs.mock.calls.length).toBeGreaterThanOrEqual(3);
      expect(mockListDrafts.mock.calls.length).toBeGreaterThanOrEqual(3);
      expect(mockListDocuments.mock.calls.length).toBeGreaterThanOrEqual(3);
      const wiki = result.current.rows.filter((r) => r.family === "wiki");
      expect(wiki.map((r) => r.key)).toEqual(["wiki-31"]);
      expect(result.current.rows.some((r) => r.family === "reindex")).toBe(false);
    }
  );

  it.each(["admin", "superadmin"])(
    "AC2: a %s principal keeps listing reindex jobs every poll",
    async (role) => {
      vi.useFakeTimers();
      publishAuthPrincipal({ id: 502, role });
      mockListReindexJobs.mockResolvedValue({
        jobs: [reindexJob(21, "running")],
      });

      const { result } = renderHook(() => useActivityJobs({ intervalMs: 1000 }));

      await flushFirstTick();
      await advanceTicks(2300);

      expect(mockListReindexJobs.mock.calls.length).toBeGreaterThanOrEqual(3);
      const reindex = result.current.rows.filter((r) => r.family === "reindex");
      expect(reindex.map((r) => r.key)).toEqual(["reindex-21"]);
      expect(reindex[0]?.title).toBe("Embedding reindex (vault 4)");
    }
  );

  it("does not arm the backoff on non-403 failures (family keeps polling)", async () => {
    vi.useFakeTimers();
    // Role is unknown (beforeEach reset); a 502-shaped normalized error must
    // not hide the family — only the admin gate's 403 may.
    mockListReindexJobs.mockRejectedValue(
      Object.assign(new Error("network down"), { status: 502 })
    );

    renderHook(() => useActivityJobs({ intervalMs: 1000 }));

    await flushFirstTick();
    await advanceTicks(2300);

    expect(mockListReindexJobs.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("backoff: a role-less principal gets exactly one 403, then the family is skipped until the principal changes", async () => {
    vi.useFakeTimers();
    // Normalized interceptor shape: top-level `status` (core.ts sets it).
    mockListReindexJobs
      .mockRejectedValueOnce(Object.assign(new Error("Admin access required"), { status: 403 }))
      .mockResolvedValue({ jobs: [] });

    renderHook(() => useActivityJobs({ intervalMs: 1000 }));

    await flushFirstTick();
    expect(mockListReindexJobs.mock.calls.length).toBe(1);

    await advanceTicks(2300);
    // Backoff armed: no further dispatch for this principal, while the
    // sibling families kept their cadence (AC3 holds under backoff too).
    expect(mockListReindexJobs.mock.calls.length).toBe(1);
    expect(mockListDocuments.mock.calls.length).toBeGreaterThanOrEqual(3);

    // A principal change (login-as switch, no reload) re-opens the family:
    // the new principal gets its own first-dispatch chance.
    publishAuthPrincipal({ id: 103_901, role: "member" });
    publishAuthPrincipal(null);
    await advanceTicks(1100);
    expect(mockListReindexJobs.mock.calls.length).toBe(2);
  });

  it("backoff: a role arriving after the first 403 overrides it (admin re-opens the family)", async () => {
    vi.useFakeTimers();
    // Auth hydrating late: first tick runs role-less and is refused...
    mockListReindexJobs
      .mockRejectedValueOnce(Object.assign(new Error("Admin access required"), { status: 403 }))
      .mockResolvedValue({ jobs: [reindexJob(22, "running", null)] });

    renderHook(() => useActivityJobs({ intervalMs: 1000 }));

    await flushFirstTick();
    expect(mockListReindexJobs.mock.calls.length).toBe(1);

    // ...then the principal lands: an admin must see the family again.
    publishAuthPrincipal({ id: 503, role: "admin" });
    await advanceTicks(1100);
    expect(mockListReindexJobs.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
