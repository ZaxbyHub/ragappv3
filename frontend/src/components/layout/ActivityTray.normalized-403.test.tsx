// frontend/src/components/layout/ActivityTray.normalized-403.test.tsx
// Issue #1069 AC6: the tray's post-403 Cancel/Retry suppression must fire
// for the error shape the api client ACTUALLY rejects with. The response
// interceptor (src/lib/api/core.ts) rejects with a normalized Error whose
// status is at the TOP level and which has NO `.response` property
// (`originalError` nests the raw axios error). The tray's `errorStatus`
// reads only `error.response.status`, so the `=== 403` gate never fires in
// production; the existing fb.test suite stayed green only because it
// fabricates the raw axios shape callers never receive.
//
// Contract asserted at BOTH ends: RED at base (the normalized-shape legs —
// top-level `status` and the `originalError.response.status`-only tier —
// never suppress the affordance), GREEN after the fix (the reader accepts
// the client's normalized shape in both nestings and stays accepting the
// raw shape — the raw-shape control leg below is green at base and must
// stay green).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";

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
    family: "wiki",
    key: "wiki-7",
    title: "Wiki compile",
    phase: "running",
    status: "running",
    terminal: false,
    cancellable: false,
    retryable: false,
    ...overrides,
  };
}

/** The api client's normalized rejection (core.ts response interceptor):
 * top-level `status`, `originalError` nesting the raw axios error, and NO
 * `.response` property of its own. */
function normalizedApiError(status: number, message: string): Error {
  return Object.assign(new Error(message), {
    status,
    originalError: { response: { status } },
  });
}

let hookRows: RowLike[];

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

describe("ActivityTray 403 suppression vs the api client's normalized error shape (#1069)", () => {
  it("stops offering Cancel after a 403 that rejects with the client's normalized shape (top-level status)", async () => {
    const user = userEvent.setup();
    mockCancelWikiJob.mockRejectedValue(
      normalizedApiError(403, "Insufficient vault permissions"),
    );
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
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

    await waitFor(
      () => {
        expect(
          within(rowByFamily("wiki")).queryByRole("button", {
            name: /cancel/i,
          }),
        ).toBeNull();
      },
      { timeout: 2000 },
    );
  });

  it("stops offering Retry after a 403 that rejects with the client's normalized shape (top-level status)", async () => {
    const user = userEvent.setup();
    mockRetryWikiJob.mockRejectedValue(
      normalizedApiError(403, "Insufficient vault permissions"),
    );
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
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

    await waitFor(
      () => {
        expect(
          within(rowByFamily("wiki")).queryByRole("button", { name: /retry/i }),
        ).toBeNull();
      },
      { timeout: 2000 },
    );
  });

  it("raw-shape control leg: a 403 carrying response.status still suppresses Cancel (the widened reader is a superset)", async () => {
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
      expect(
        within(rowByFamily("wiki")).queryByRole("button", { name: /cancel/i }),
      ).toBeNull();
    });
  });

  it("originalError-only leg: a 403 nested as originalError.response.status (no top-level status, no response) also suppresses Cancel", async () => {
    // The three-tier chain's third tier: an error whose ONLY status carrier
    // is the raw axios error nested under `originalError` — the shape a
    // caller gets when the interceptor's normalized fields are absent but
    // the original rejection is still attached. Currently unguarded: the
    // reader reads neither `status` nor `originalError.response.status`.
    const user = userEvent.setup();
    mockCancelWikiJob.mockRejectedValue(
      Object.assign(new Error("Insufficient vault permissions"), {
        originalError: { response: { status: 403 } },
      }),
    );
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
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

    await waitFor(
      () => {
        expect(
          within(rowByFamily("wiki")).queryByRole("button", {
            name: /cancel/i,
          }),
        ).toBeNull();
      },
      { timeout: 2000 },
    );
  });

  it("a normalized NON-403 failure keeps the affordance (widening must not over-suppress)", async () => {
    const user = userEvent.setup();
    mockCancelWikiJob.mockRejectedValue(
      normalizedApiError(503, "service unavailable"),
    );
    hookRows = [
      makeRow({
        family: "wiki",
        key: "wiki-7",
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
      expect(mockToastError).toHaveBeenCalledWith("service unavailable");
    });
    expect(
      within(rowByFamily("wiki")).queryByRole("button", { name: /cancel/i }),
    ).not.toBeNull();
  });
});
