import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { updateMessageFeedback } from "@/lib/api";
import { AssistantMessageActions, UserMessageActions } from "./MessageActions";

vi.mock("@/lib/api", () => ({
  updateMessageFeedback: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
  },
}));

describe("AssistantMessageActions feedback", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(localStorage.getItem).mockReturnValue(null);
    vi.mocked(updateMessageFeedback).mockResolvedValue({} as Awaited<ReturnType<typeof updateMessageFeedback>>);
  });

  it("sends the selected feedback rating to the API", () => {
    render(<AssistantMessageActions content="Answer" sessionId="7" messageId="42" />);

    fireEvent.click(screen.getByLabelText("Good response"));

    expect(updateMessageFeedback).toHaveBeenCalledWith(7, 42, "up");
  });

  it("cycles feedback between up, down, and cleared", () => {
    render(<AssistantMessageActions content="Answer" sessionId="7" messageId="42" />);

    const good = screen.getByLabelText("Good response");
    const bad = screen.getByLabelText("Bad response");

    fireEvent.click(good);
    expect(good).toHaveAttribute("aria-pressed", "true");
    expect(updateMessageFeedback).toHaveBeenLastCalledWith(7, 42, "up");

    fireEvent.click(bad);
    expect(bad).toHaveAttribute("aria-pressed", "true");
    expect(updateMessageFeedback).toHaveBeenLastCalledWith(7, 42, "down");

    fireEvent.click(bad);
    expect(bad).toHaveAttribute("aria-pressed", "false");
    expect(updateMessageFeedback).toHaveBeenLastCalledWith(7, 42, null);
  });

  it("rolls back to resolved external feedback when save fails", async () => {
    vi.mocked(updateMessageFeedback).mockRejectedValueOnce(new Error("offline"));
    const onFeedback = vi.fn();

    render(
      <AssistantMessageActions
        content="Answer"
        sessionId="7"
        messageId="42"
        externalFeedback="up"
        onFeedback={onFeedback}
      />
    );

    fireEvent.click(screen.getByLabelText("Bad response"));

    expect(onFeedback).toHaveBeenCalledWith("down");
    await waitFor(() => expect(onFeedback).toHaveBeenLastCalledWith("up"));
    expect(localStorage.setItem).toHaveBeenLastCalledWith("chat_feedback_42", "up");
    expect(toast.error).toHaveBeenCalledWith("Couldn't save feedback");
  });

  it("uses localStorage only when the server has no feedback value", () => {
    vi.mocked(localStorage.getItem).mockReturnValue("up");

    const { rerender } = render(
      <AssistantMessageActions content="Answer" sessionId="7" messageId="42" />
    );

    expect(screen.getByLabelText("Good response")).toHaveAttribute("aria-pressed", "true");

    rerender(
      <AssistantMessageActions content="Answer" sessionId="7" messageId="42" serverFeedback={null} />
    );

    expect(screen.getByLabelText("Good response")).toHaveAttribute("aria-pressed", "false");
    expect(localStorage.removeItem).toHaveBeenCalledWith("chat_feedback_42");
  });

  it("ignores a stale vote failure after a newer vote succeeded (UI-050)", async () => {
    // Vote 1 ("up") stays in flight; vote 2 ("down") resolves. When the older
    // request finally rejects, the per-message sequence guard must swallow it —
    // the newer saved selection must not roll back.
    let rejectFirst!: (err: Error) => void;
    const firstVote = new Promise<never>((_, reject) => {
      rejectFirst = reject;
    });
    vi.mocked(updateMessageFeedback)
      .mockReset()
      .mockReturnValueOnce(firstVote)
      .mockResolvedValueOnce({} as Awaited<ReturnType<typeof updateMessageFeedback>>);
    const onFeedback = vi.fn();

    render(
      <AssistantMessageActions
        content="Answer"
        sessionId="7"
        messageId="42"
        onFeedback={onFeedback}
      />
    );

    // Click "up" (request 1 stays pending)...
    fireEvent.click(screen.getByLabelText("Good response"));
    expect(screen.getByLabelText("Good response")).toHaveAttribute("aria-pressed", "true");
    expect(onFeedback).toHaveBeenLastCalledWith("up");

    // ...then click "down" (request 2 resolves and supersedes it).
    fireEvent.click(screen.getByLabelText("Bad response"));
    expect(screen.getByLabelText("Bad response")).toHaveAttribute("aria-pressed", "true");
    expect(onFeedback).toHaveBeenLastCalledWith("down");

    // Now the STALE first request fails.
    await act(async () => {
      rejectFirst(new Error("stale vote failure"));
    });

    // Final selection stays "down" — not reverted to the stale request's view.
    expect(screen.getByLabelText("Bad response")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Good response")).toHaveAttribute("aria-pressed", "false");
    expect(onFeedback).toHaveBeenLastCalledWith("down");
    // localStorage keeps the newer vote; no rollback write happened.
    expect(localStorage.setItem).toHaveBeenLastCalledWith("chat_feedback_42", "down");
    expect(localStorage.setItem).not.toHaveBeenLastCalledWith("chat_feedback_42", "up");
    expect(localStorage.removeItem).not.toHaveBeenCalledWith("chat_feedback_42");
    // The stale failure is not surfaced as a user-visible error.
    expect(toast.error).not.toHaveBeenCalled();
  });
});

describe("UserMessageActions", () => {
  it("disables edit while generation is streaming", async () => {
    const onEdit = vi.fn();
    render(
      <UserMessageActions
        content="question"
        onEdit={onEdit}
        isEditDisabled
      />
    );

    const edit = screen.getByRole("button", { name: "Edit message" });
    expect(edit).toBeDisabled();

    await userEvent.click(edit);
    expect(onEdit).not.toHaveBeenCalled();
  });
});
