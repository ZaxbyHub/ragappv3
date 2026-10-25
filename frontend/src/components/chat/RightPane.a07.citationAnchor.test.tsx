// frontend/src/components/chat/RightPane.a07.citationAnchor.test.tsx
// A07 (AC4): clicking a citation chip IN message 2 must anchor the evidence
// pane to message 2's sources. AssistantMessage's handleSourceClick writes
// selectedEvidenceSource + selectedEvidenceMessageId(msg2.id) + activeRightTab
// "evidence" to the shell store; the rendered RightPane (evidence/sources
// tab) must then list msg2's sources (x and z), never msg1's (x and y). On
// the current tree useSourcesForSourceId scans messageIds from the front and
// returns the FIRST message containing the source id — msg1 — so the pane
// shows msg1's list.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { RightPane } from "./RightPane";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
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

const apiMocks = vi.hoisted(() => ({
  getChunkContext: vi.fn(),
  getDocumentRawBlob: vi.fn(),
  getArtifactRawBlob: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getChunkContext: (...args: unknown[]) => apiMocks.getChunkContext(...args),
  getDocumentRawBlob: (...args: unknown[]) => apiMocks.getDocumentRawBlob(...args),
  getArtifactRawBlob: (...args: unknown[]) => apiMocks.getArtifactRawBlob(...args),
}));

const sourceX: Source = { id: "chunk-x", filename: "x-title" };
const sourceY: Source = { id: "chunk-y", filename: "y-title" };
const sourceZ: Source = { id: "chunk-z", filename: "z-title" };

describe("RightPane citation anchor (A07 AC4)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // #865 forceMount: the context panel mounts eagerly with the Sources
    // tab, so its selection effect calls getChunkContext before any test
    // override — default it to a settled no-context response.
    apiMocks.getChunkContext.mockResolvedValue(undefined);
    useChatStore.setState({
      messageIds: ["msg1", "msg2"],
      messagesById: {
        msg1: {
          id: "msg1",
          role: "assistant",
          content: "first answer cites [S1] and [S2]",
          status: "complete",
          sources: [sourceX, sourceY],
        },
        msg2: {
          id: "msg2",
          role: "assistant",
          content: "second answer cites [S1] and [S3]",
          status: "complete",
          sources: [sourceX, sourceZ],
        },
      },
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
      rightPaneOpen: false,
    });
  });

  it("evidence pane shows the clicked message's sources", () => {
    // The user clicks source x IN msg2 — exactly the store writes
    // AssistantMessage's handleSourceClick performs for that chip.
    const shell = useChatShellStore.getState();
    shell.setSelectedEvidenceSource(sourceX);
    shell.setSelectedEvidenceMessageId("msg2");
    shell.setEvidenceReturnFocusId("msg2");
    shell.setActiveRightTab("evidence");
    shell.openRightPane();

    render(<RightPane />);

    // The pane lists msg2's sources: x and z present, y (msg1-only) absent.
    // #865 forceMount: the Preview panel is mounted (hidden) alongside the
    // Sources list, so the selected source's title can appear twice in the
    // DOM — scope the count pins to the Sources list itself.
    const sourcesList = screen
      .getAllByText("z-title")[0]
      .closest('[aria-label="Sources list"]') as HTMLElement | null;
    expect(sourcesList).not.toBeNull();
    const withinSources = sourcesList as HTMLElement;
    expect(within(sourcesList).queryAllByText("z-title").length).toBe(1);
    expect(within(withinSources).queryAllByText("x-title").length).toBe(1);
    expect(within(withinSources).queryAllByText("y-title").length).toBe(0);
  });
});
