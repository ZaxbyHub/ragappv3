// frontend/src/components/chat/AssistantMessage.currency-warning.test.tsx
// Issue #510 AC-17 / UI-004: currency warnings from the done payload render
// as a persistent amber advisory, and a missing_citations enforcement result
// renders a visible warning — satisfied stays silent.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { AssistantMessage } from "./AssistantMessage";
import { useChatShellStore } from "@/stores/useChatShellStore";
import type { Message } from "@/stores/useChatStore";

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(),
}));

const createMessage = (overrides: Partial<Message> = {}): Message => ({
  id: "msg-currency-1",
  role: "assistant",
  content: "Answer drawn from older sources.",
  ...overrides,
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

describe("AssistantMessage currency warnings (issue #510 AC-17)", () => {
  it("renders a Currency Warnings advisory listing each warning", () => {
    render(
      <AssistantMessage
        message={createMessage({
          currencyWarnings: [
            "S1 may be superseded by S2",
            "S3 is older than the currency window",
          ],
        })}
      />
    );

    expect(screen.getByText("Currency Warnings")).toBeInTheDocument();
    expect(screen.getByText("S1 may be superseded by S2")).toBeInTheDocument();
    expect(screen.getByText("S3 is older than the currency window")).toBeInTheDocument();
  });

  it("renders no advisory when currencyWarnings is absent", () => {
    render(<AssistantMessage message={createMessage()} />);

    expect(screen.queryByText("Currency Warnings")).not.toBeInTheDocument();
  });

  it("renders no advisory when currencyWarnings is an empty array", () => {
    render(<AssistantMessage message={createMessage({ currencyWarnings: [] })} />);

    expect(screen.queryByText("Currency Warnings")).not.toBeInTheDocument();
  });
});

describe("AssistantMessage citation enforcement display (issue #510 UI-004)", () => {
  it("renders a visible warning when enforcement reports missing_citations", () => {
    render(
      <AssistantMessage
        message={createMessage({
          citationEnforcement: {
            mode: "required",
            status: "missing_citations",
            detail: "Citations were required but the answer contains no valid citation labels.",
          },
        })}
      />
    );

    expect(
      screen.getByText("Citations were required but none were found in this answer")
    ).toBeInTheDocument();
  });

  it("stays silent when enforcement is satisfied or absent", () => {
    const { rerender } = render(<AssistantMessage message={createMessage()} />);
    expect(
      screen.queryByText("Citations were required but none were found in this answer")
    ).not.toBeInTheDocument();

    rerender(
      <AssistantMessage
        message={createMessage({
          citationEnforcement: { mode: "required", status: "satisfied" },
        })}
      />
    );
    expect(
      screen.queryByText("Citations were required but none were found in this answer")
    ).not.toBeInTheDocument();
  });
});
