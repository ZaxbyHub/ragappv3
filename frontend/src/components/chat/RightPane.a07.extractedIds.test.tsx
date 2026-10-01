// frontend/src/components/chat/RightPane.a07.extractedIds.test.tsx
// A07 (AC7): the extracted-outputs view derives each code block's React key
// from its first 20 non-whitespace characters. Two fenced blocks that share
// that prefix but differ in full content collide on one key, so React logs
// its duplicate-key console.error when the Extracted tab renders. The pane
// must give same-prefix blocks distinct keys.

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

const fence = "```";
// Both blocks' first 20 non-whitespace characters are "constalpha=computeAl"
// but the full contents differ.
const samePrefixAnswer = [
  "Two helpers follow.",
  "",
  `${fence}python`,
  "const alpha = computeAlpha()",
  "return alpha",
  fence,
  "",
  `${fence}python`,
  "const alpha = computeAlphabet()",
  "return beta",
  fence,
  "",
].join("\n");

describe("RightPane extracted-output keys (A07 AC7)", () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    useChatStore.setState({
      messageIds: ["a1"],
      messagesById: {
        a1: {
          id: "a1",
          role: "assistant",
          content: samePrefixAnswer,
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

  it("same-prefix code blocks get distinct keys", () => {
    render(<RightPane />);

    // Two structured outputs exist, so the Extracted tab is enabled.
    expect(screen.getByText("(2)")).toBeInTheDocument();

    // Activate the Extracted tab (Radix tabs activate on Enter/Space keyDown
    // and on mousedown) — this mounts the extracted-outputs list.
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
