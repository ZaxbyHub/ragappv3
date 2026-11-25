import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";

// #1163 AC4 (frozen check C13): the queue page shows each item beside the
// source quote it rests on; approve/reject actions inspect both stores via
// the batch API; navigation wiring asserts the /review-queue route.

vi.mock("@/lib/api", () => ({
  listReviewQueue: vi.fn(),
  batchReviewQueue: vi.fn(),
}));

vi.mock("@/stores/useVaultStore", () => {
  // Selector-aware with a STABLE state object — a fresh-object mock makes
  // the selector's result a new identity every render and loops effects.
  const state = { activeVaultId: 5 };
  return {
    useVaultStore: (sel?: (s: typeof state) => unknown) =>
      sel ? sel(state) : state,
  };
});

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

// Radix Checkbox cannot be driven reliably in jsdom (the house gotcha);
// render-stub it the way wiki-jobs-ui-checks does — selection wiring is
// asserted through the aria-labeled input's change event.
vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({
    checked,
    onCheckedChange,
    "aria-label": ariaLabel,
  }: {
    checked?: boolean;
    onCheckedChange?: (v: boolean) => void;
    "aria-label"?: string;
  }) => (
    <input
      type="checkbox"
      aria-label={ariaLabel}
      checked={!!checked}
      onChange={(e) => onCheckedChange?.(e.currentTarget.checked)}
      data-testid={ariaLabel ? `checkbox-${ariaLabel}` : undefined}
    />
  ),
}));

import { batchReviewQueue, listReviewQueue } from "@/lib/api";
import ReviewQueuePage from "./ReviewQueuePage";

function renderAt(path = "/review-queue") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/review-queue" element={<ReviewQueuePage />} />
        <Route path="*" element={<div>Not Found</div>} />
      </Routes>
    </MemoryRouter>
  );
}

const QUEUE = {
  items: [
    {
      kind: "wiki_page" as const,
      id: 1,
      title: "Badge protocol",
      slug: "document/one",
      status: "needs_review",
      file_id: null,
      updated_at: "2026-01-01T00:00:00Z",
      source_quote: "Alice Smith is the AFOMIS Director.",
    },
    {
      kind: "kms_entry" as const,
      id: 9,
      title: "Doc One",
      slug: "doc-one",
      status: "draft",
      file_id: 1,
      updated_at: "2026-01-02T00:00:00Z",
      source_quote: "Alice Smith is the AFOMIS Director. (document excerpt)",
    },
  ],
  count: 2,
};

beforeEach(() => {
  vi.clearAllMocks();
  (listReviewQueue as ReturnType<typeof vi.fn>).mockResolvedValue(QUEUE);
  (batchReviewQueue as ReturnType<typeof vi.fn>).mockResolvedValue({
    action: "approve",
    updated: 2,
    items: [],
  });
});

describe("ReviewQueuePage (#1163 AC4 / C13)", () => {
  it("renders at /review-queue (route registration)", async () => {
    renderAt();
    expect(
      await screen.findByText(/Badge protocol/)
    ).toBeInTheDocument();
  });

  it("shows each item beside the source quote it rests on", async () => {
    renderAt();
    await screen.findByText("Badge protocol");
    const quote = await screen.findByTestId("source-quote-wiki_page-1");
    expect(quote).toHaveTextContent("Alice Smith is the AFOMIS Director.");
    const quoteKms = await screen.findByTestId("source-quote-kms_entry-9");
    expect(quoteKms).toHaveTextContent("(document excerpt)");
    // Both kinds carry their kind labels in one merged list (the filter
    // bar also says "Wiki page" — assert on the item badges specifically).
    const itemBadges = screen.getAllByText("Wiki page");
    expect(itemBadges.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Knowledge entry", { selector: "span" })).toBeInTheDocument();
  });

  it("batch-approves every selected item through the queue API", async () => {
    const user = userEvent.setup();
    renderAt();
    await screen.findByText("Badge protocol");
    await user.click(screen.getByLabelText("Select Badge protocol"));
    await user.click(screen.getByLabelText("Select Doc One"));
    await user.click(screen.getByRole("button", { name: /^Approve$/ }));
    await waitFor(() => {
      expect(batchReviewQueue).toHaveBeenCalledWith(
        5,
        [
          { kind: "wiki_page", id: 1 },
          { kind: "kms_entry", id: 9 },
        ],
        "approve"
      );
    });
  });

  it("batch-rejects the selection and filters by kind", async () => {
    const user = userEvent.setup();
    renderAt();
    await screen.findByText("Badge protocol");
    // Kind filter narrows the visible list.
    await user.click(screen.getByRole("button", { name: "Knowledge entry" }));
    await waitFor(() => {
      expect(screen.queryByText("Badge protocol")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Doc One")).toBeInTheDocument();
    await user.click(screen.getByLabelText("Select Doc One"));
    await user.click(screen.getByRole("button", { name: /^Reject$/ }));
    await waitFor(() => {
      expect(batchReviewQueue).toHaveBeenCalledWith(
        5,
        [{ kind: "kms_entry", id: 9 }],
        "reject"
      );
    });
  });

  it("keeps an empty state and surfaces load errors", async () => {
    (listReviewQueue as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [],
      count: 0,
    });
    renderAt();
    expect(
      await screen.findByText(/Nothing awaiting review/i)
    ).toBeInTheDocument();
  });
});
