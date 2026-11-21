// frontend/src/components/layout/ActivityTray.audit1115.collapsed.test.tsx
//
// Arm's-length acceptance check for the activity-API trace (audit
// 1115-1157-1158): the tray's collapsed state must persist across remounts
// via localStorage key "activity-tray-collapsed" ("true"/"false", absent =>
// expanded), mirroring NavigationRail.tsx's lazy-initializer + write-back
// pattern (its sidebar-expanded key). At base the tray's collapsed state is
// component-local useState(false): a remount re-expands and nothing is ever
// written to localStorage, so the persistence leg is RED at base and turns
// GREEN with the fix. The absent-key leg (expanded) pins the default so the
// fix cannot invert it.
//
// Harness mirrors ActivityTray.fb.test.tsx (vi.hoisted mock factories +
// module replacement, empty-stores zustand stand-in per
// ActivityTray.retryIdempotency.test.tsx) with one live wiki row so the
// expanded/collapsed distinction is observable through listitems.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockUseActivityJobs, mockCancelWikiJob, mockRetryWikiJob } = vi.hoisted(
  () => ({
    mockUseActivityJobs: vi.fn(),
    mockCancelWikiJob: vi.fn(),
    mockRetryWikiJob: vi.fn(),
  }),
);

vi.mock("@/hooks/useJobStatus", () => ({
  useActivityJobs: mockUseActivityJobs,
}));

vi.mock("@/lib/api/wiki", () => ({
  cancelWikiJob: mockCancelWikiJob,
  retryWikiJob: mockRetryWikiJob,
}));

vi.mock("@/lib/api/draftRoom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/draftRoom")>()),
  cancelDraftJob: vi.fn(),
  retryDraftJob: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  cancelDocumentIngest: vi.fn(),
}));

// Empty-stores zustand stand-in (same idiom as
// ActivityTray.retryIdempotency.test.tsx:59-71).
vi.mock("@/stores/useUploadStore", () => {
  const emptyUploadState = {
    uploads: [],
    isProcessing: false,
    activeVaultId: null,
    chatAttachmentIds: [],
  };
  return {
    useUploadStore: (selector?: (state: typeof emptyUploadState) => unknown) =>
      selector ? selector(emptyUploadState) : emptyUploadState,
    phaseLabelFor: (phase?: string | null) => phase ?? null,
  };
});

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), info: vi.fn() },
}));

import { ActivityTray } from "./ActivityTray";

const TRAY_KEY = "activity-tray-collapsed";

// The suite-level setup (src/test/setup.ts) replaces global localStorage
// with no-op vi.fn()s, so a persistence check must install a BACKED store
// first — the same Object.defineProperty sham pattern as
// useChatShellStore.pinStorageSync.test.ts. Without it every getItem is
// structurally null and the persistence contract is unobservable.
function installBackingLocalStorage(): void {
  const backing = new Map<string, string>();
  const sham = {
    getItem: (key: string) => backing.get(key) ?? null,
    setItem: (key: string, value: string) => void backing.set(key, value),
    removeItem: (key: string) => void backing.delete(key),
    clear: () => backing.clear(),
    key: (index: number) => [...backing.keys()][index] ?? null,
    get length() {
      return backing.size;
    },
  };
  Object.defineProperty(window, "localStorage", {
    value: sham,
    writable: true,
    configurable: true,
  });
}

function renderTray() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <ActivityTray />
    </QueryClientProvider>,
  );
}

function activityRegion(): HTMLElement {
  return screen.getByRole("region", { name: "Activity" });
}

beforeEach(() => {
  installBackingLocalStorage();
  window.localStorage.removeItem(TRAY_KEY);
  mockUseActivityJobs.mockImplementation(() => ({
    rows: [
      {
        family: "wiki" as const,
        key: "wiki-31",
        title: "Wiki compile",
        phase: "running",
        status: "running",
        terminal: false,
        cancellable: false,
        retryable: false,
        jobId: 31,
        vaultId: 1,
      },
    ],
    loading: false,
    refresh: vi.fn(),
  }));
  mockCancelWikiJob.mockReset();
  mockRetryWikiJob.mockReset();
});

describe("ActivityTray collapsed-preference persistence (audit 1115)", () => {
  it("persists the collapsed state across remounts under the exact localStorage key", async () => {
    const user = userEvent.setup();

    // Expanded by default: the row is listed.
    const first = renderTray();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);

    // Collapse via the Hide control inside the Activity region.
    await user.click(
      within(activityRegion()).getByRole("button", { name: /hide/i }),
    );
    expect(screen.queryByRole("listitem")).toBeNull();
    // The exact key and value are part of the contract.
    expect(window.localStorage.getItem(TRAY_KEY)).toBe("true");

    // Remount (route change / full re-render): still collapsed.
    first.unmount();
    renderTray();
    expect(activityRegion()).toBeDefined();
    expect(screen.queryByRole("listitem")).toBeNull();
    // The header is still present so the user can re-expand.
    expect(
      within(activityRegion()).getByRole("button", { name: /activity/i }),
    ).toBeDefined();

    // Re-expand via the header and verify the row comes back (and the
    // preference follows the new state).
    await user.click(
      within(activityRegion()).getByRole("button", { name: /activity/i }),
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(window.localStorage.getItem(TRAY_KEY)).toBe("false");
  });

  it("treats an absent localStorage key as expanded (no stored value => rows visible)", () => {
    window.localStorage.removeItem(TRAY_KEY);
    renderTray();

    expect(activityRegion()).toBeDefined();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(
      within(activityRegion()).getByRole("button", { name: /hide/i }),
    ).toBeDefined();
  });
});
