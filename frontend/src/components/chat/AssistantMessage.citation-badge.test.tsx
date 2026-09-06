// frontend/src/components/chat/AssistantMessage.citation-badge.test.tsx
// Issue #508 WU-11: AssistantMessage forwards validated citation labels to
// SourceCards (Cited/Retrieved badges) and stamps the chip focus anchor with
// the owning message id.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { AssistantMessage } from "./AssistantMessage";
import { useChatShellStore } from "@/stores/useChatShellStore";
import type { Message } from "@/stores/useChatStore";
import type { Source } from "@/lib/api";

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(),
}));

const SOURCES: Source[] = [
  { id: "s1", filename: "a.pdf", source_label: "S1" },
  { id: "s2", filename: "b.pdf", source_label: "S2" },
];

const createMessage = (overrides: Partial<Message> = {}): Message => ({
  id: "msg-badge-1",
  role: "assistant",
  content: "Cites [S1] and [S2].",
  sources: SOURCES,
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

describe("AssistantMessage citation badge + chip anchor forwarding (issue #508)", () => {
  it("derives validCitationLabels from citationConfidence keys and renders Cited/Retrieved badges", () => {
    // Backend semantics: citation_confidence carries one entry per VALID
    // [S#] label — only S1 was validated as cited here.
    render(
      <AssistantMessage
        message={createMessage({ citationConfidence: { S1: 0.42 } })}
      />
    );

    expect(screen.getByText("Cited")).toBeInTheDocument();
    expect(screen.getByText("Retrieved")).toBeInTheDocument();
    expect(screen.queryByText("verified")).not.toBeInTheDocument();
  });

  it("renders no badge when the message has no citationConfidence", () => {
    render(<AssistantMessage message={createMessage()} />);

    expect(screen.queryByText("Cited")).not.toBeInTheDocument();
    expect(screen.queryByText("Retrieved")).not.toBeInTheDocument();
  });

  it("renders no badge when citationConfidence is an empty object", () => {
    // {} and undefined both mean "nothing validated" — neither may show a
    // badge (an empty set would flip every card to "Retrieved").
    render(<AssistantMessage message={createMessage({ citationConfidence: {} })} />);

    expect(screen.queryByText("Cited")).not.toBeInTheDocument();
    expect(screen.queryByText("Retrieved")).not.toBeInTheDocument();
  });

  it("stamps the chip focus anchor with the owning message id", () => {
    render(<AssistantMessage message={createMessage()} />);

    const chip = document.querySelector('[data-citation-chip-message="msg-badge-1"]');
    expect(chip).not.toBeNull();
  });
});
