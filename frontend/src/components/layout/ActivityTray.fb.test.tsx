// frontend/src/components/layout/ActivityTray.fb.test.tsx
// Feedback round 853-20261005 (PR #853 swarm review): assertions the
// frozen m04 contract does not carry and that must live OUTSIDE the frozen
// files (they are byte-locked by the trace checkpoint). Covers:
// - PRR-041: kms/reindex rows render NO Cancel and failed ingest rows
//   render no Retry (negative-button assertions).
// - PRR-042: family labels are asserted ROW-SCOPED, not document-scoped.
// - PRR-043 / PRR-006 / PRR-005: the Hide control collapses the list;
//   a clicked action's button is disabled while in flight; a Cancel
//   refused with 403 disappears from that row (learned affordance); a
//   non-403 failure surfaces a toast.
// - PRR-019: source tripwire — the tray must never import the client
//   upload store (the "server-sourced" constraint regression guard).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const {
  mockUseActivityJobs,
  mockCancelWikiJob,
  mockRetryWikiJob,
  mockToastError,
} = vi.hoisted(() => ({
  mockUseActivityJobs: vi.fn(),
  mockCancelWikiJob: vi.fn(),
  mockRetryWikiJob: vi.fn(),
  mockToastError: vi.fn(),
}));

vi.mock("@/hooks/useJobStatus", () => ({
  useActivityJobs: mockUseActivityJobs,
}));

vi.mock("@/lib/api/wiki", () => ({
  cancelWikiJob: mockCancelWikiJob,
  retryWikiJob: mockRetryWikiJob,
}));

vi.mock("@/lib/api/draftRoom", () => ({
  cancelDraftJob: vi.fn(),
  retryDraftJob: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  cancelDocumentIngest: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: { error: mockToastError, success: vi.fn(), info: vi.fn() },
}));

// Empty-stores zustand stand-in (the tray must not read it at all — the
// source tripwire below asserts that; this mock only keeps an accidental
// import from crashing the test instead of silently passing).
vi.mock("@/stores/useUploadStore", () => {
  const emptyUploadState = { uploads: [] };
  return {
    useUploadStore: (selector?: (state: typeof emptyUploadState) => unknown) =>
      selector ? selector(emptyUploadState) : emptyUploadState,
  };
});

import { ActivityTray } from "./ActivityTray";

interface RowLike {
  family: "ingest" | "wiki" | "draft-room" | "kms" | "reindex";
  key: string;
  title: string;
  phase: string;
  status: string;
  terminal: boolean;
  cancellable: boolean;
  retryable: boolean;
  jobId?: number;
  vaultId?: number;
  fileId?: string;
}

function makeRow(overrides: Partial<RowLike> = {}): RowLike {
  return {
    family: "ingest",
    key: "row-1",
    title: "Row 1",
    phase: "running",
    status: "running",
    terminal: false,
    cancellable: false,
    retryable: false,
    ...overrides,
  };
}

let hookRows: RowLike[];

function rowByTitle(title: string): HTMLElement {
  const row = screen
    .getAllByRole("listitem")
    .find((li) => li.textContent?.includes(title));
  expect(row, `no listitem row found for title ${title}`).toBeDefined();
  return row as HTMLElement;
}

function rowByFamily(family: string): HTMLElement {
  const row = screen
    .getAllByRole("listitem")
    .find((li) => li.textContent?.toLowerCase().includes(family.toLowerCase()));
  expect(row, `no listitem row found for family ${family}`).toBeDefined();
  return row as HTMLElement;
}

beforeEach(() => {
  hookRows = [];
  mockUseActivityJobs.mockImplementation(() => ({
    rows: hookRows,
    loading: false,
    refresh: vi.fn(),
  }));
  mockCancelWikiJob.mockReset();
  mockRetryWikiJob.mockReset();
  mockToastError.mockReset();
});

describe("ActivityTray feedback-round assertions (853-20261005)", () => {
  it("offers no Cancel on kms/reindex rows and no Retry on live ingest rows", () => {
    hookRows = [
      makeRow({
        family: "kms",
        key: "kms-11",
        title: "KMS recompile",
        status: "running",
      }),
      makeRow({
        family: "reindex",
        key: "reindex-21",
        title: "Embedding reindex (vault 4)",
        status: "running",
      }),
      makeRow({
        family: "ingest",
        key: "ingest-42",
        title: "handbook.pdf",
        status: "processing",
        cancellable: true,
        fileId: "42",
      }),
    ];
    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    expect(
      within(rowByFamily("kms")).queryByRole("button", { name: /cancel/i }),
    ).toBeNull();
    expect(
      within(rowByFamily("kms")).queryByRole("button", { name: /retry/i }),
    ).toBeNull();
    expect(
      within(rowByFamily("reindex")).queryByRole("button", { name: /cancel/i }),
    ).toBeNull();
    expect(
      within(rowByFamily("ingest")).queryByRole("button", { name: /retry/i }),
    ).toBeNull();
    expect(
      within(rowByFamily("ingest")).getByRole("button", { name: /cancel/i }),
    ).toBeDefined();
  });

  it("asserts family labels row-scoped (each family text inside its own listitem)", () => {
    hookRows = [
      makeRow({ family: "ingest", key: "i-1", title: "a.pdf" }),
      makeRow({ family: "wiki", key: "w-1", title: "Wiki compile" }),
      makeRow({ family: "draft-room", key: "d-1", title: "Draft 1" }),
      makeRow({ family: "kms", key: "k-1", title: "KMS recompile" }),
      makeRow({ family: "reindex", key: "r-1", title: "Embedding reindex" }),
    ];
    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    for (const family of ["ingest", "wiki", "draft-room", "kms", "reindex"]) {
      const row = rowByFamily(family);
      expect(
        within(row).getAllByText(new RegExp(family, "i")).length,
      ).toBeGreaterThan(0);
    }
  });

  it("collapses the list via Hide and keeps the count visible while collapsed", async () => {
    const user = userEvent.setup();
    hookRows = [makeRow({ family: "wiki", key: "w-1", title: "Wiki compile" })];
    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: /hide/i }));

    expect(screen.queryByRole("listitem")).toBeNull();
    expect(screen.getByText(/activity/i)).toBeDefined();
    // Re-expand via the expand-only header button.
    await user.click(screen.getByRole("button", { name: /activity/i }));
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("disables the clicked action's button while its request is in flight", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    mockCancelWikiJob.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ job_id: 7, status: "cancelled" });
        }),
    );
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        cancellable: true,
        jobId: 7,
        vaultId: 3,
      }),
    ];
    const { rerender } = render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    const cancel = within(rowByFamily("wiki")).getByRole("button", {
      name: /cancel/i,
    });
    await user.click(cancel);
    expect(cancel).toBeDisabled();

    act(() => {
      release();
    });
    await waitFor(() => expect(cancel).not.toBeDisabled());
  });

  it("stops offering Cancel on a row the server refused with 403 (learned affordance)", async () => {
    const user = userEvent.setup();
    mockCancelWikiJob.mockRejectedValue(
      Object.assign(new Error("Insufficient vault permissions"), {
        response: { status: 403 },
      }),
    );
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        cancellable: true,
        jobId: 7,
        vaultId: 3,
      }),
    ];
    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    const cancel = within(rowByFamily("wiki")).getByRole("button", {
      name: /cancel/i,
    });
    await user.click(cancel);

    await waitFor(() => {
      expect(
        within(rowByFamily("wiki")).queryByRole("button", { name: /cancel/i }),
      ).toBeNull();
    });
  });

  it("stops offering Retry on a row the server refused with 403 (learned affordance covers retry)", async () => {
    const user = userEvent.setup();
    mockRetryWikiJob.mockRejectedValue(
      Object.assign(new Error("Insufficient vault permissions"), {
        response: { status: 403 },
      }),
    );
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        phase: "failed",
        status: "failed",
        terminal: true,
        retryable: true,
        jobId: 7,
        vaultId: 3,
      }),
    ];
    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    await user.click(
      within(rowByFamily("wiki")).getByRole("button", { name: /retry/i }),
    );

    await waitFor(() => {
      expect(
        within(rowByFamily("wiki")).queryByRole("button", { name: /retry/i }),
      ).toBeNull();
    });
  });

  it("surfaces a non-403 action failure as an error toast", async () => {
    const user = userEvent.setup();
    mockCancelWikiJob.mockRejectedValue(new Error("network down"));
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        cancellable: true,
        jobId: 7,
        vaultId: 3,
      }),
    ];
    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    await user.click(
      within(rowByFamily("wiki")).getByRole("button", { name: /cancel/i }),
    );

    await waitFor(() => {
      expect(mockToastError).toHaveBeenCalledWith("network down");
    });
    // A non-403 failure does NOT remove the affordance.
    expect(
      within(rowByFamily("wiki")).queryByRole("button", { name: /cancel/i }),
    ).not.toBeNull();
  });

  it("triggers the hook refresh after an action settles (finally-refresh is asserted, not decorative)", async () => {
    const user = userEvent.setup();
    const mockRefresh = vi.fn();
    mockUseActivityJobs.mockImplementation(() => ({
      rows: [
        makeRow({
          family: "wiki",
          key: "wiki-7",
          title: "Wiki compile",
          cancellable: true,
          jobId: 7,
          vaultId: 3,
        }),
      ],
      loading: false,
      refresh: mockRefresh,
    }));
    mockCancelWikiJob.mockResolvedValue({ job_id: 7, status: "cancelled" });
    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    await user.click(
      within(rowByFamily("wiki")).getByRole("button", { name: /cancel/i }),
    );

    await waitFor(() => {
      expect(mockRefresh).toHaveBeenCalledTimes(1);
    });
  });

  it("restores focus to the tray header when the focused row is removed by a poll tick", async () => {
    const user = userEvent.setup();
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
        title: "Wiki compile",
        cancellable: true,
        jobId: 7,
        vaultId: 3,
      }),
      makeRow({ family: "kms", key: "kms-11", title: "KMS recompile" }),
    ];
    const { rerender } = render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    // Focus a control inside the focused row (negative fixture first: focus
    // OUTSIDE the tray must never be stolen by the guard).
    await user.click(
      within(rowByFamily("wiki")).getByRole("button", { name: /cancel/i }),
    );
    // Resolve the click's async continuation so the button is re-enabled
    // (mockCancelWikiJob resolves in beforeEach). The in-flight disable can
    // clear activeElement to body in jsdom, so REFOCUS explicitly — the
    // pre-removal state must be deterministic, not timing-dependent.
    const cancelControl = within(rowByFamily("wiki")).getByRole("button", {
      name: /cancel/i,
    });
    await waitFor(() => expect(cancelControl).toBeEnabled());
    act(() => cancelControl.focus());
    // #1158 planning-pin: show focus on the SPECIFIC row control immediately
    // before the removal — "somewhere in the tray" is not the contract.
    expect(document.activeElement).toBe(cancelControl);

    // Poll tick: the focused row vanishes (cancelled rows drop from the
    // pending/processing windows the adapters poll).
    hookRows = [
      makeRow({ family: "kms", key: "kms-11", title: "KMS recompile" }),
    ];
    rerender(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    const header = screen.getByRole("button", { name: /activity/i });
    await waitFor(() => {
      expect(document.activeElement).toBe(header);
    });

    // Negative case (#1158 focus machine): a ROW is focused, focus then
    // moves DELIBERATELY to an external control (a concrete relatedTarget
    // outside the tray), and only then does the poll remove the focused
    // row — the guard must NOT steal focus back to the header.
    const wikiRow = makeRow({
      family: "wiki",
      key: "wiki-7",
      title: "Wiki compile",
      cancellable: true,
      jobId: 7,
      vaultId: 3,
    });
    hookRows = [wikiRow, makeRow({ family: "kms", key: "kms-11", title: "KMS recompile" })];
    rerender(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );
    const externalCancel =
      within(rowByFamily("wiki")).getByRole("button", { name: /cancel/i });
    await user.click(externalCancel);
    // jsdom's in-flight disable clears activeElement to body, and when the
    // re-enable lands after user.click's act envelope nothing refocuses the
    // re-enabled button — the same gap the positive fixture refocuses
    // around. Make the pre-blur state explicit (deterministic, not
    // timing-dependent).
    await waitFor(() => expect(externalCancel).toBeEnabled());
    act(() => externalCancel.focus());
    await waitFor(() => {
      expect(document.activeElement).toBe(externalCancel);
    });
    const bodyTarget = document.createElement("button");
    document.body.appendChild(bodyTarget);
    // A real user event (not .focus()) so the focusout carries a concrete
    // external relatedTarget — the deliberate-move signature.
    await user.click(bodyTarget);
    // Poll tick removes the previously focused row: external focus stays.
    hookRows = [];
    rerender(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );
    expect(document.activeElement).toBe(bodyTarget);
    bodyTarget.remove();
  });

  // R8 (r2, execution-proven): a deadzone/body click blurs the focused row
  // control with a NULL relatedTarget — the same signature as node removal.
  // The tray must disambiguate (node still connected = deliberate blur), so
  // a later poll-tick removal never yanks focus from where the user moved
  // it (here: an outside input).
  it("does not steal focus after a deliberate body-click blur when the focused row later drops", async () => {
    const user = userEvent.setup();
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-12",
        title: "Wiki compile",
        cancellable: true,
        jobId: 12,
        vaultId: 3,
      }),
    ];
    const { rerender } = render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    const cancel = within(rowByFamily("wiki")).getByRole("button", {
      name: /cancel/i,
    });
    await user.click(cancel);
    const r8Cancel = within(rowByFamily("wiki")).getByRole("button", {
      name: /cancel/i,
    });
    await waitFor(() => expect(r8Cancel).toBeEnabled());
    act(() => r8Cancel.focus());
    expect(document.activeElement).toBe(r8Cancel);

    // Deadzone-click blur: focusout with a NULL relatedTarget while the
    // blurred node stays connected (the browser's non-focusable-click
    // signature; jsdom does not move focus on body clicks, so the events
    // are dispatched directly, mirroring the executed Chrome probe).
    cancel.dispatchEvent(
      new FocusEvent("focusout", { relatedTarget: null, bubbles: true }),
    );
    (cancel as HTMLButtonElement).blur();
    // Flush the tray's disambiguation macrotask: the still-connected node
    // marks the blur deliberate and clears the in-tray marker.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const outside = document.createElement("input");
    document.body.appendChild(outside);
    outside.focus();
    expect(document.activeElement).toBe(outside);

    // Poll tick removes the stale-focused row: focus must stay put.
    hookRows = [];
    rerender(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );
    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  // r1-critic F-4: the guard's refs are read/written inside the effect (no
  // render-phase writes), so a StrictMode double render cannot make
  // prev == current and silently disable restoration.
  it("restores focus under StrictMode when the focused row is removed", async () => {
    const user = userEvent.setup();
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-13",
        title: "Wiki compile",
        cancellable: true,
        jobId: 13,
        vaultId: 3,
      }),
    ];
    const { rerender } = render(
      <StrictMode>
        <QueryClientProvider
          client={
            new QueryClient({ defaultOptions: { queries: { retry: false } } })
          }
        >
          <ActivityTray />
        </QueryClientProvider>
      </StrictMode>,
    );

    await user.click(
      within(rowByFamily("wiki")).getByRole("button", { name: /cancel/i }),
    );
    const smCancel = within(rowByFamily("wiki")).getByRole("button", {
      name: /cancel/i,
    });
    await waitFor(() => expect(smCancel).toBeEnabled());
    act(() => smCancel.focus());
    expect(document.activeElement).toBe(smCancel);

    hookRows = [];
    rerender(
      <StrictMode>
        <QueryClientProvider
          client={
            new QueryClient({ defaultOptions: { queries: { retry: false } } })
          }
        >
          <ActivityTray />
        </QueryClientProvider>
      </StrictMode>,
    );

    const header = screen.getByRole("button", { name: /activity/i });
    await waitFor(() => {
      expect(document.activeElement).toBe(header);
    });
  });

  // R8 (Stage B): a null-blur disambiguation callback scheduled for row A
  // must not clear the marker after row B has already received focus — a
  // stale callback would disable B's later restoration.
  it("keeps the in-tray marker when a newer row focuses before an older blur callback fires", async () => {
    const user = userEvent.setup();
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-20",
        title: "Wiki compile A",
        cancellable: true,
        jobId: 20,
        vaultId: 3,
      }),
      makeRow({
        family: "wiki",
        key: "wiki-21",
        title: "Wiki compile B",
        cancellable: true,
        jobId: 21,
        vaultId: 3,
      }),
    ];
    const { rerender } = render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    const cancelA = within(rowByTitle("Wiki compile A")).getByRole("button", {
      name: /cancel/i,
    });
    await user.click(cancelA);
    await waitFor(() => expect(cancelA).toBeEnabled());
    act(() => cancelA.focus());
    expect(document.activeElement).toBe(cancelA);

    // Blur row A with a null relatedTarget (schedules the callback).
    cancelA.dispatchEvent(
      new FocusEvent("focusout", { relatedTarget: null, bubbles: true }),
    );
    // A SECOND null blur pends from the tray itself (header focuses then
    // blurs with null — its focusout also bubbles through the section), so
    // TWO callbacks are pending when B focuses. The in-tray focus must
    // cancel BOTH (r2-reviewer multi-callback sequence).
    const header = screen.getByRole("button", { name: /activity/i });
    act(() => header.focus());
    header.dispatchEvent(
      new FocusEvent("focusout", { relatedTarget: null, bubbles: true }),
    );

    // Row B receives focus BEFORE either callback fires (cancel both).
    const cancelB = within(rowByTitle("Wiki compile B")).getByRole("button", {
      name: /cancel/i,
    });
    act(() => cancelB.focus());
    // Flush: the stale callback must be a no-op for B's marker.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(document.activeElement).toBe(cancelB);

    // Row B drops: the marker survived, so focus restores to the header.
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-20",
        title: "Wiki compile",
        cancellable: true,
        jobId: 20,
        vaultId: 3,
      }),
    ];
    rerender(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      expect(document.activeElement).toBe(header);
    });
  });

  // Stage B test-engineer (R8 keep-polarity): the blurred node is
  // DISCONNECTED before the macrotask fires (the removal signature) — the
  // callback must KEEP the marker so the removal restores focus to the
  // header. Observable in jsdom because the node is detached explicitly
  // between dispatch and fire (real browsers fire this focusout on removal).
  it("keeps the marker when the blurred node is disconnected before the macrotask fires", async () => {
    const user = userEvent.setup();
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-30",
        title: "Wiki compile",
        cancellable: true,
        jobId: 30,
        vaultId: 3,
      }),
    ];
    const { rerender } = render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );

    const cancel = within(rowByFamily("wiki")).getByRole("button", {
      name: /cancel/i,
    });
    await user.click(cancel);
    await waitFor(() => expect(cancel).toBeEnabled());
    act(() => cancel.focus());
    expect(document.activeElement).toBe(cancel);

    // Null blur schedules the callback...
    cancel.dispatchEvent(
      new FocusEvent("focusout", { relatedTarget: null, bubbles: true }),
    );
    // ...and the node is disconnected BEFORE it fires (removal signature).
    cancel.remove();
    // Flush the macrotask: the disconnected node must KEEP the marker
    // (mutant polarity: an unconditional clear would disable the restore).
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The removal itself (rows change) triggers the restore effect.
    hookRows = [];
    rerender(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ActivityTray />
      </QueryClientProvider>,
    );
    const header = screen.getByRole("button", { name: /activity/i });
    await waitFor(() => {
      expect(document.activeElement).toBe(header);
    });
  });

  it("never imports the client upload store (server-sourced constraint tripwire)", () => {
    const traySource = readFileSync(
      path.join(
        path.dirname(fileURLToPath(import.meta.url)),
        "ActivityTray.tsx",
      ),
      "utf8",
    );
    expect(traySource).not.toMatch(/useUploadStore|stores\/useUploadStore/);
  });
});
