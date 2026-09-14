// frontend/src/components/chat/AssistantMessage.reasoning.test.tsx
// Issue #554 AC6 / AC8: a message carrying a typed reasoning part renders a
// collapsible "Thinking for Ns" block — collapsed by default, expands on
// click, and shows the duration (seconds) and the token estimate when open.
// The reasoning display reads from message.parts (typed parts model), not
// another ad-hoc Message field; the static mode badge stays untouched.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { AssistantMessage } from "./AssistantMessage";
import { useChatShellStore } from "@/stores/useChatShellStore";
import type { Message } from "@/stores/useChatStore";

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(),
}));

const createMessage = (overrides: Partial<Message> = {}): Message => ({
  id: "msg-reasoning-1",
  role: "assistant",
  content: "The retention period is 500 hours.",
  ...overrides,
});

const REASONING_MESSAGE = createMessage({
  parts: [
    {
      kind: "reasoning",
      text: "The user asks about retention; I should check the maintenance section.",
      durationMs: 4200,
      tokensEstimate: 37,
    },
    { kind: "text", text: "The retention period is 500 hours." },
    { kind: "source", source: { id: "s1", filename: "handbook.pdf" } },
  ] as unknown as NonNullable<Message["parts"]>,
});

beforeEach(() => {
  vi.clearAllMocks();
  (useChatShellStore as unknown as vi.Mock).mockReturnValue({
    openRightPane: vi.fn(),
    setSelectedEvidenceSource: vi.fn(),
    setSelectedEvidenceMessageId: vi.fn(),
    setEvidenceReturnFocusId: vi.fn(),
    setActiveRightTab: vi.fn(),
  });
});

describe("AssistantMessage reasoning block (issue #554 AC6/AC8)", () => {
  it("shows a collapsed 'Thinking for Ns' toggle and hides the reasoning text by default", () => {
    render(<AssistantMessage message={REASONING_MESSAGE} />);

    const toggle = screen.queryByText("Thinking for 4s");
    expect(
      toggle,
      "AC8-C8: a reasoning part must render a collapsed-by-default 'Thinking for Ns' toggle (duration in seconds)"
    ).toBeTruthy();
    expect(
      screen.queryByText(/check the maintenance section/),
      "AC8-C8: the reasoning text must be hidden while the block is collapsed"
    ).toBeNull();
  });

  it("expands on click to reveal the reasoning text and the token estimate", () => {
    render(<AssistantMessage message={REASONING_MESSAGE} />);

    fireEvent.click(screen.getByText("Thinking for 4s"));

    expect(
      screen.getByText(/check the maintenance section/),
      "AC8-C8: clicking the toggle must expand the reasoning text"
    ).toBeTruthy();
    expect(
      screen.getByText("~37 tokens"),
      "AC8-C8: the expanded block must show the labeled token estimate"
    ).toBeTruthy();
  });

  it("renders no thinking block when the message has no reasoning part", () => {
    render(<AssistantMessage message={createMessage()} />);

    expect(
      screen.queryByText(/^Thinking for /),
      "AC6-C6: messages without a typed reasoning part must not render a thinking block"
    ).toBeNull();
  });

  it("renders no thinking block when parts exist but contain no reasoning part", () => {
    render(
      <AssistantMessage
        message={createMessage({
          parts: [
            { kind: "text", text: "The retention period is 500 hours." },
            { kind: "source", source: { id: "s1", filename: "handbook.pdf" } },
          ] as unknown as NonNullable<Message["parts"]>,
        })}
      />
    );

    expect(
      screen.queryByText(/^Thinking for /),
      "AC6-C6: only a typed reasoning part may render the thinking block"
    ).toBeNull();
  });
});
