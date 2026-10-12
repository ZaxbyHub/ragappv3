import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api/draftRoom", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/draftRoom")>(
      "@/lib/api/draftRoom",
    );
  return {
    ...actual,
    deleteDraftInput: vi.fn(),
    updateDraftInput: vi.fn(),
    getDraftInputContent: vi.fn(),
  };
});

vi.mock("@/components/ui/select", async () => {
  const actual = await vi.importActual<typeof import("@/components/ui/select")>(
    "@/components/ui/select",
  );
  return actual;
});

import { deleteDraftInput, parseDraftRoomError } from "@/lib/api/draftRoom";
import { DraftSourceList } from "./DraftSourceList";

const mockDelete = vi.mocked(deleteDraftInput);

/** Build the normalized-error shape `parseDraftRoomError` actually reads. */
function apiError(status: number, code: string, detail = "boom"): unknown {
  const err = new Error(detail) as Error & {
    status: number;
    originalError: { response: { data: { detail: string; code: string } } };
  };
  err.status = status;
  err.originalError = { response: { data: { detail, code } } };
  return err;
}

const INPUT = {
  id: 7,
  original_name: "manuscript.txt",
  role: "reference",
  authority: "unknown",
  as_of_date: null,
  locked_spans: null,
  parse_status: "ready",
  parse_error: null,
  size_bytes: 12,
  media_type: "text/plain",
  storage_relpath: "1/1/inputs/7/a.txt",
  created_at: "2026-01-01T00:00:00Z",
} as unknown as Parameters<typeof DraftSourceList>[0]["inputs"][number];

function Harness(props: Parameters<typeof DraftSourceList>[0]): ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>{<DraftSourceList {...props} />}</QueryClientProvider>
  );
}

const baseProps = {
  draftId: 1,
  inputs: [INPUT],
  locked: false,
  lockedReason: undefined,
  canEdit: true,
};

describe("DraftSourceList delete guards (#785 PRR-004 / PRR-016)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("disables the Remove control while the room is disabled", () => {
    render(<Harness {...baseProps} editCapabilityBlocked={true} />);

    const remove = screen.getByRole("button", { name: /remove manuscript\.txt/i });
    expect(remove).toBeDisabled();
  });

  it("keeps the Remove control enabled when the room is enabled", () => {
    render(<Harness {...baseProps} editCapabilityBlocked={false} />);

    const remove = screen.getByRole("button", { name: /remove manuscript\.txt/i });
    expect(remove).toBeEnabled();
  });

  it("defaults to not blocking, so callers that pass nothing are unaffected", () => {
    render(<Harness {...baseProps} />);

    const remove = screen.getByRole("button", { name: /remove manuscript\.txt/i });
    expect(remove).toBeEnabled();
  });

  it("announces a 503 refusal inside the dialog instead of only in a toast", async () => {
    const user = userEvent.setup();
    mockDelete.mockRejectedValueOnce(apiError(503, "draft_room_disabled"));
    render(<Harness {...baseProps} />);

    await user.click(screen.getByRole("button", { name: /remove manuscript\.txt/i }));
    await user.click(screen.getByRole("button", { name: /^remove$/i }));

    // The refusal stays where focus already is: a role="alert" message inside
    // the still-open dialog, carrying the deployment-specific wording.
    const alert = await screen.findByRole("alert");
    // The deployment-specific wording replaces the raw server detail, so a
    // mutation that swaps DRAFT_ROOM_DISABLED_MESSAGE for parsed.detail fails.
    expect(alert.textContent ?? "").toContain("not enabled on this deployment");
    expect(alert.textContent ?? "").not.toContain("draft room is disabled");
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // And the dialog is not dismissed by the failure.
    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });
  });

  it("surfaces the server's reason for a 409 provenance refusal in the dialog", async () => {
    const user = userEvent.setup();
    mockDelete.mockRejectedValueOnce(
      apiError(409, "input_in_use", "input was used by a compile and may back derived artifacts"),
    );
    render(<Harness {...baseProps} />);

    await user.click(screen.getByRole("button", { name: /remove manuscript\.txt/i }));
    await user.click(screen.getByRole("button", { name: /^remove$/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toContain("used by a compile");
  });

  it("keeps parseDraftRoomError's contract used by the handler", () => {
    // Guards the assumption the onError branch relies on.
    const parsed = parseDraftRoomError(apiError(503, "draft_room_disabled", "x"));
    expect(parsed.code).toBe("draft_room_disabled");
    expect(parsed.status).toBe(503);
  });
});