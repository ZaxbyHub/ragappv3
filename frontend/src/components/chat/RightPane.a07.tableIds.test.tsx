// frontend/src/components/chat/RightPane.a07.tableIds.test.tsx
// Issue #689 implementation-review Finding 1: extracted-output ids are
// push-ordinal based (`outputs.length`), so a message containing a
// pipe-TERMINATED table followed by a final UNTERMINATED table (running to
// end of content) must yield distinct React keys — the ordinal-counter
// variant of the fix regressed exactly this shape (both took ordinal N).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { RightPane } from "./RightPane";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";

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

// First table is terminated by a non-pipe line; second runs to end of
// content (the EOF branch of extractStructuredOutputs).
const terminatedPlusUnterminated = [
  "Two tables follow.",
  "",
  "| a | b |",
  "|---|---|",
  "| 1 | 2 |",
  "",
  "between",
  "",
  "| c | d |",
  "|---|---|",
  "| 3 | 4 |",
].join("\n");

describe("RightPane extracted-output table keys (issue #689 review F1)", () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    useChatStore.setState({
      messageIds: ["t1"],
      messagesById: {
        t1: {
          id: "t1",
          role: "assistant",
          content: terminatedPlusUnterminated,
          status: "complete",
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

  afterEach(() => {
    consoleErrorSpy.mockRestore();
  });

  it("terminated table plus trailing unterminated table get distinct keys", () => {
    render(<RightPane />);

    // Two structured outputs exist, so the Extracted tab is enabled.
    expect(screen.getByText("(2)")).toBeInTheDocument();

    // Activate the Extracted tab (Radix tabs activate on Enter/Space
    // keyDown and on mousedown) — this mounts the extracted-outputs list.
    fireEvent.keyDown(screen.getByRole("tab", { name: /extracted/i }), {
      key: "Enter",
    });

    const duplicateKeyWarnings = consoleErrorSpy.mock.calls
      .map((call) => call[0])
      .filter(
        (arg) => typeof arg === "string" && /same key|duplicate key/i.test(arg)
      );
    expect(duplicateKeyWarnings).toEqual([]);
  });
});
