/**
 * @vitest-environment jsdom
 *
 * RED at base (master), GREEN after the fix. AC2 of issue #718: Evidence pane
 * renders chunk-context neighbor text exactly once.
 *
 * WHEN the Evidence pane opens a source whose chunk-context response carries
 * neighbor text in `before` / `after` that is absent from `context_text`, the
 * pane SHALL render that neighbor text exactly once. At base RightPane only
 * renders `context_text`, so the neighbor text appears 0 times (RED: expected
 * 0 to be 1).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { RightPane } from "./RightPane";
import * as useChatStoreModule from "@/stores/useChatStore";
import * as useChatShellStoreModule from "@/stores/useChatShellStore";
import { getChunkContext, getArtifactRawBlob, type ChunkContextResponse } from "@/lib/api";

// jsdom does not implement scrollIntoView; the evidence-selection scroll
// effect fires it from a timer, so stub it before any test renders.
Element.prototype.scrollIntoView = vi.fn();

// Mock the stores (mirrors RightPane.test.tsx verbatim)
vi.mock("@/stores/useChatStore", () => {
  const chatStoreMock: any = vi.fn();
  chatStoreMock.getState = () => {
    const state = chatStoreMock() ?? {};
    const messagesById: Record<string, any> = {};
    for (const m of state.messages ?? []) {
      if (m?.id) messagesById[String(m.id)] = m;
    }
    return { ...state, messagesById };
  };
  const messagesFromMock = (): any[] => {
    const state = chatStoreMock();
    return state?.messages ?? [];
  };
  const useChatMessagesMock = vi.fn(messagesFromMock);
  const useLastCompletedAssistantSourcesMock = vi.fn(() => {
    const messages = messagesFromMock();
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i];
      if (msg?.role === "assistant" && msg?.sources) return msg.sources;
    }
    return undefined;
  });
  const useLastUserContentMock = vi.fn(() => {
    const messages = messagesFromMock();
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i]?.role === "user") return messages[i].content ?? "";
    }
    return "";
  });
  const useSourcesForSourceIdMock = vi.fn((sourceId?: string) => {
    if (!sourceId) return undefined;
    const messages = messagesFromMock();
    for (const msg of messages) {
      if (msg?.sources?.some((s: any) => s?.id === sourceId)) {
        return msg.sources;
      }
    }
    return undefined;
  });
  const useCompletedAssistantMessageIdsKeyMock = vi.fn(() => {
    const messages = messagesFromMock();
    const ids = messages
      .filter((m: any) => m?.role === "assistant")
      .map((m: any) => m.id ?? "");
    return JSON.stringify(ids);
  });
  const useLastCompletedAssistantWikiRefsMock = vi.fn(() => undefined);
  const useLastCompletedAssistantKmsRefsMock = vi.fn(() => undefined);
  // Issue #965: one-line stub (sibling pattern) — the real selector's
  // scan-back is pinned in useChatStore.selectors.test.ts and the dedicated
  // c09 pane suite; no assertion in this file observes its output.
  const useLastCompletedAssistantCitationConfidenceMock = vi.fn(() => undefined);
  const useStreamingCandidateSourcesMock = vi.fn(() => {
    const state = chatStoreMock() ?? {};
    const id = (state as { streamingMessageId?: string }).streamingMessageId;
    if (!id) return undefined;
    const messages = messagesFromMock();
    return messages.find((m: any) => m?.id === id)?.candidateSources;
  });
  return {
    useChatStore: chatStoreMock,
    useChatMessages: useChatMessagesMock,
    useLastCompletedAssistantSources: useLastCompletedAssistantSourcesMock,
    useLastCompletedAssistantWikiRefs: useLastCompletedAssistantWikiRefsMock,
    useLastCompletedAssistantKmsRefs: useLastCompletedAssistantKmsRefsMock,
    useLastCompletedAssistantCitationConfidence: useLastCompletedAssistantCitationConfidenceMock,
    useLastUserContent: useLastUserContentMock,
    useSourcesForSourceId: useSourcesForSourceIdMock,
    useCompletedAssistantMessageIdsKey: useCompletedAssistantMessageIdsKeyMock,
    useStreamingCandidateSources: useStreamingCandidateSourcesMock,
    parseCompletedAssistantIds: (key: string) => {
      if (!key) return [];
      try { const p = JSON.parse(key); return Array.isArray(p) ? p.map(String) : []; } catch { return []; }
    },
  };
});

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(() => ({
    selectedEvidenceSource: null,
    setSelectedEvidenceSource: vi.fn(),
    activeRightTab: "evidence",
    setActiveRightTab: vi.fn(),
  })),
}));

vi.mock("@/lib/api", () => ({
  getChunkContext: vi.fn().mockResolvedValue({
    id: "src-c13",
    file_id: "1",
    filename: "c13-doc.pdf",
    chunk_index: 0,
    chunk_text: "The cited chunk body text",
    context_text: "Loaded parent-window context without the neighbor sentences.",
    context_source: "parent_window",
  }),
  getArtifactRawBlob: vi.fn().mockResolvedValue(
    new Blob(["fake-png"], { type: "image/png" })
  ),
}));

// Mock UI components with proper interactivity (mirrors RightPane.test.tsx)
const mockOnValueChange = vi.fn();

vi.mock("@/components/ui/tabs", () => ({
  Tabs: ({ children, value, onValueChange }: any) => {
    // Store callback for tests
    if (onValueChange) {
      mockOnValueChange.mockImplementation(onValueChange);
    }
    return (
      <div data-testid="tabs" data-value={value}>
        {children}
      </div>
    );
  },
  TabsList: ({ children }: any) => <div data-testid="tabs-list">{children}</div>,
  TabsTrigger: ({ children, value, disabled, onClick, ...props }: any) => (
    <button
      data-testid={`tab-${value}`}
      disabled={disabled}
      onClick={onClick}
      {...props}
    >
      {children}
    </button>
  ),
  TabsContent: ({ children, value }: any) => (
    <div data-testid={`tab-content-${value}`}>{children}</div>
  ),
}));

vi.mock("@/components/ui/scroll_area", () => ({
  ScrollArea: ({ children }: any) => <div data-testid="scroll-area">{children}</div>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, onClick, variant, size, ...props }: any) => (
    <button
      data-testid={props["data-testid"]}
      onClick={onClick}
      {...props}
    >
      {children}
    </button>
  ),
}));

const mockUseChatStore = useChatStoreModule.useChatStore as unknown as ReturnType<
  typeof vi.fn
>;

const mockUseChatShellStore = useChatShellStoreModule.useChatShellStore as unknown as ReturnType<
  typeof vi.fn
>;

// Test helpers (mirrors RightPane.test.tsx)
const createMockSource = (overrides = {}) => ({
  id: `source-${Math.random().toString(36).substr(2, 9)}`,
  filename: "test-document.pdf",
  snippet: "This is a test snippet for the document",
  score: 0.15,
  score_type: "distance" as const,
  ...overrides,
});

const createMockMessage = (overrides = {}) => ({
  id: `msg-${Math.random().toString(36).substr(2, 9)}`,
  role: "user" as const,
  content: "Test message content",
  ...overrides,
});

// Chunk-context payload for AC2: the response carries neighbor chunks in
// `before` / `after` that are ABSENT from `context_text`. The neighbor fields
// are part of the issue #718 contract; the double cast keeps the literal
// assignable to the current ChunkContextResponse shape both before the fix
// (fields absent from the interface) and after it (fields present).
const NEIGHBOR_BEFORE_TEXT = "NEIGHBOR-BEFORE-C13";
const NEIGHBOR_AFTER_TEXT = "NEIGHBOR-AFTER-C13";
const CONTEXT_TEXT = "Loaded parent-window context without the neighbor sentences.";
const neighborContextPayload = {
  id: "src-c13",
  file_id: "1",
  filename: "c13-doc.pdf",
  chunk_index: 0,
  chunk_text: "The cited chunk body text",
  context_text: CONTEXT_TEXT,
  context_source: "parent_window",
  before: [NEIGHBOR_BEFORE_TEXT],
  after: [NEIGHBOR_AFTER_TEXT],
} as unknown as ChunkContextResponse;

describe("RightPane chunk-context neighbor text (issue #718 AC2)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockOnValueChange.mockClear();
    vi.mocked(getChunkContext).mockResolvedValue(neighborContextPayload);
    (mockUseChatShellStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      selectedEvidenceSource: null,
      setSelectedEvidenceSource: vi.fn(),
      activeRightTab: "evidence",
      setActiveRightTab: vi.fn(),
    });
  });

  it("renders before/after neighbor text absent from context_text exactly once", async () => {
    const sources = [
      // Snippet deliberately absent from CONTEXT_TEXT so the support-span
      // split never fragments the preview text nodes under test.
      createMockSource({
        id: "src-c13",
        filename: "c13-doc.pdf",
        snippet: "The cited chunk body text",
      }),
    ];

    mockUseChatStore.mockReturnValue({
      // Query terms chosen to appear nowhere in the context or neighbor
      // texts, so no <mark> splitting interferes with the text queries.
      messages: [
        createMockMessage({ role: "user", content: "unrelated question" }),
        createMockMessage({ role: "assistant", content: "response", sources }),
      ],
      expandedSources: new Set(),
    });

    render(<RightPane />);

    // Open the source's context view from the Evidence list (same mechanics
    // as RightPane.test.tsx: click the source card, land on the Preview tab).
    const sourceButton = screen.getAllByRole("button").find(btn =>
      btn.textContent?.includes("c13-doc.pdf")
    );
    expect(sourceButton).toBeInTheDocument();
    fireEvent.click(sourceButton!);

    // Wait for the mocked chunk-context response to be loaded and rendered,
    // so the neighbor-text assertions run against the loaded pane, not a
    // spinner or the saved snippet.
    await waitFor(() => {
      expect(getChunkContext).toHaveBeenCalledWith("src-c13");
    });
    await waitFor(() => {
      expect(screen.getByText(CONTEXT_TEXT)).toBeInTheDocument();
    });

    // AC2: neighbor text carried in `before` / `after` (absent from
    // context_text) must be rendered exactly once each.
    expect(screen.queryAllByText(new RegExp(NEIGHBOR_BEFORE_TEXT)).length).toBe(1);
    expect(screen.queryAllByText(new RegExp(NEIGHBOR_AFTER_TEXT)).length).toBe(1);
  });
});
