// frontend/src/components/chat/RightPane.a07.loadingReset.test.tsx
// A07 (AC8): in the source preview, a source whose context fetch is still
// pending shows the "Loading context" spinner. Switching the selection to a
// synthesized source (metadata.synthesized) must clear that spinner — the
// synthesized preview skips the context fetch entirely. On the current tree
// the effect's synthesized early-return happens before any loading-state
// reset, and the previous fetch's cancelled .finally never lands, so the
// spinner sticks forever.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import { RightPane } from "./RightPane";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import type { Source } from "@/lib/api";

// Mock ResizeObserver for Radix UI ScrollArea; jsdom omits scrollIntoView.
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

const pendingSource: Source = {
  id: "chunk-pending",
  file_id: "11",
  filename: "slow-context.pdf",
  snippet: "The pending chunk's excerpt.",
};
const synthesizedSource: Source = {
  id: "chunk-synth",
  filename: "Synthesized from 3 sources",
  snippet: "A condensed summary.",
  metadata: { synthesized: true },
};

describe("RightPane synthesized-source loading reset (A07 AC8)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The context fetch for the pending source never settles.
    apiMocks.getChunkContext.mockImplementation(() => new Promise(() => {}));
    useChatStore.setState({
      messageIds: ["a1"],
      messagesById: {
        a1: {
          id: "a1",
          role: "assistant",
          content: "answer cites both [S1] and [S2]",
          status: "complete",
          sources: [pendingSource, synthesizedSource],
        },
      },
      streamingMessageId: null,
      activeChatId: "7",
      isStreaming: false,
      expandedSources: new Set(),
    });
    useChatShellStore.setState({
      selectedEvidenceSource: pendingSource,
      selectedEvidenceMessageId: "a1",
      evidenceReturnFocusId: "a1",
      activeRightTab: "preview",
      rightPaneOpen: true,
    });
  });

  it("switching to a synthesized source clears the spinner", async () => {
    render(<RightPane />);

    // The pending source's preview shows the context-loading spinner.
    await waitFor(() => {
      expect(screen.queryAllByText("Loading context").length).toBe(1);
    });

    // Switch the selection to the synthesized source.
    act(() => {
      useChatShellStore.getState().setSelectedEvidenceSource(synthesizedSource);
    });

    // The synthesized preview renders (no chunk fetch for it)...
    await waitFor(() => {
      expect(
        screen.queryAllByText("Synthesized from 3 sources").length
      ).toBeGreaterThan(0);
    });
    // ...and no context-loading indicator survives the switch.
    expect(screen.queryAllByText("Loading context").length).toBe(0);
  });
});
