// frontend/src/components/chat/citationReporting.m02.test.tsx
// Issue-trace 782-firstrun-checklist-vaultgate-kms-wiki (unfrozen pin):
// opening a citation fires the first-run milestone report from BOTH chat
// surfaces — AssistantMessage.handleSourceClick (the funnel for chips and
// source cards) and RightPane's evidence-list handler. Store mocking follows
// the real-store setState pattern of RightPane.a07.citationAnchor.test.tsx.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";

const onboardingMocks = vi.hoisted(() => ({
  reportCitationOpened: vi.fn().mockResolvedValue(undefined),
  getOnboardingMilestones: vi.fn(),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

vi.mock("@/lib/api/onboarding", () => onboardingMocks);

const apiMocks = vi.hoisted(() => ({
  getChunkContext: vi.fn().mockResolvedValue(null),
  getDocumentRawBlob: vi.fn().mockResolvedValue(null),
  getArtifactRawBlob: vi.fn().mockResolvedValue(null),
}));

vi.mock("@/lib/api", () => ({
  getChunkContext: (...args: unknown[]) => apiMocks.getChunkContext(...args),
  getDocumentRawBlob: (...args: unknown[]) =>
    apiMocks.getDocumentRawBlob(...args),
  getArtifactRawBlob: (...args: unknown[]) =>
    apiMocks.getArtifactRawBlob(...args),
}));

import { AssistantMessage } from "./AssistantMessage";
import { RightPane } from "./RightPane";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import type { Message } from "@/stores/useChatStore";
import type { Source } from "@/lib/api";

// Mock ResizeObserver for Radix UI ScrollArea; jsdom omits scrollIntoView and
// the evidence-selection scroll effect fires it from a timer.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
Element.prototype.scrollIntoView = vi.fn();
Element.prototype.scrollTo = vi.fn();

const SOURCE: Source = { id: "s1", filename: "handbook.pdf", source_label: "S1" };

const createMessage = (overrides: Partial<Message> = {}): Message => ({
  id: "msg-cite-1",
  role: "assistant",
  content: "Answer cites [S1].",
  sources: [SOURCE],
  status: "complete",
  ...overrides,
});

describe("citation-open milestone reporting (issue #782)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatShellStore.setState({
      selectedEvidenceSource: null,
      selectedEvidenceMessageId: null,
      evidenceReturnFocusId: null,
      activeRightTab: "evidence",
      rightPaneOpen: false,
      selectedSource: null,
      activeTab: "preview",
    });
  });

  it("AssistantMessage's source-card click reports the citation open", async () => {
    const user = userEvent.setup();
    render(<AssistantMessage message={createMessage()} />);

    await user.click(screen.getByText("handbook.pdf"));
    expect(onboardingMocks.reportCitationOpened).toHaveBeenCalled();
  });

  it("RightPane's evidence-list click reports the citation open", async () => {
    useChatStore.setState({
      messageIds: ["msg1"],
      messagesById: { msg1: createMessage({ id: "msg1" }) },
      streamingMessageId: null,
      activeChatId: "7",
      isStreaming: false,
      expandedSources: new Set(),
    });
    useChatShellStore.setState({
      selectedEvidenceSource: null,
      selectedEvidenceMessageId: null,
      evidenceReturnFocusId: null,
      activeRightTab: "evidence",
      rightPaneOpen: true,
    });
    const user = userEvent.setup();
    render(<RightPane />);

    await user.click(screen.getByText("handbook.pdf"));
    expect(onboardingMocks.reportCitationOpened).toHaveBeenCalled();
  });
});
