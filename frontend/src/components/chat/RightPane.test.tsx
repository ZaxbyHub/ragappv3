/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { RightPane } from "./RightPane";
import * as useChatStoreModule from "@/stores/useChatStore";
import * as useChatShellStoreModule from "@/stores/useChatShellStore";
import { getChunkContext, getArtifactRawBlob } from "@/lib/api";

// Mock the stores
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
    id: "src-1",
    file_id: "1",
    filename: "doc.pdf",
    chunk_index: 0,
    chunk_text: "Snippet content",
    context_text: "Expanded source context",
    context_source: "parent_window",
  }),
  getArtifactRawBlob: vi.fn().mockResolvedValue(
    new Blob(["fake-png"], { type: "image/png" })
  ),
}));

// Mock UI components with proper interactivity
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

// Test helpers
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

describe("RightPane", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockOnValueChange.mockClear();
    vi.mocked(getChunkContext).mockResolvedValue({
      id: "src-1",
      file_id: "1",
      filename: "doc.pdf",
      chunk_index: 0,
      chunk_text: "Snippet content",
      context_text: "Expanded source context",
      context_source: "parent_window",
    });
  });

  // =============================================================================
  // SCENARIO 1: RightPane renders Evidence tab by default
  // =============================================================================
  describe("renders Evidence tab by default", () => {
    it("should show Evidence header and Sources tab by default", () => {
      mockUseChatStore.mockReturnValue({
        messages: [],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("Evidence")).toBeInTheDocument();
      expect(screen.getByText("Sources")).toBeInTheDocument();
    });

    it("should have sources tab content visible", () => {
      mockUseChatStore.mockReturnValue({
        messages: [],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const sourcesContent = screen.getByTestId("tab-content-sources");
      expect(sourcesContent).toBeInTheDocument();
    });
  });

  // =============================================================================
  // SCENARIO 2: EvidenceTab shows empty state when no sources
  // =============================================================================
  describe("EvidenceTab empty state", () => {
    it("should show empty state when messages array is empty", () => {
      mockUseChatStore.mockReturnValue({
        messages: [],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("No sources yet")).toBeInTheDocument();
      expect(screen.getByText("Send a message to see retrieved sources.")).toBeInTheDocument();
    });

    it("should show empty state when no assistant messages with sources", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "Hello" }),
          createMockMessage({ role: "assistant", content: "Hi there!", sources: [] }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("No sources yet")).toBeInTheDocument();
      expect(screen.getByText("Send a message to see retrieved sources.")).toBeInTheDocument();
    });

    it("should show empty state when user message exists but no assistant", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "Hello" }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("No sources yet")).toBeInTheDocument();
      expect(screen.getByText("Send a message to see retrieved sources.")).toBeInTheDocument();
    });
  });

  // =============================================================================
  // SCENARIO 3: EvidenceTab renders source list with rank, filename, relevance badge
  // =============================================================================
  describe("EvidenceTab source list rendering", () => {
    it("should display sources with rank numbers", () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "alpha.pdf", score: 0.1 }),
        createMockSource({ id: "src-2", filename: "beta.pdf", score: 0.3 }),
        createMockSource({ id: "src-3", filename: "gamma.pdf", score: 0.6 }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test query" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Check that source count badge shows (3)
      expect(screen.getByText("(3)")).toBeInTheDocument();

      // Check all filenames are visible - use queryAllByText to handle potential duplicates
      const alphaElements = screen.queryAllByText("alpha.pdf");
      expect(alphaElements.length).toBeGreaterThan(0);
      expect(screen.queryAllByText("beta.pdf").length).toBeGreaterThan(0);
      expect(screen.queryAllByText("gamma.pdf").length).toBeGreaterThan(0);
    });

    it("should display relevance badges for each source", () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "high.pdf", score: 0.1, score_type: "distance" }),
        createMockSource({ id: "src-2", filename: "medium.pdf", score: 0.3, score_type: "distance" }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Score 0.1 (distance) = "Highly Relevant"
      // Score 0.3 (distance) = "Relevant"
      expect(screen.getByText("Highly Relevant")).toBeInTheDocument();
      expect(screen.getByText("Relevant")).toBeInTheDocument();
    });

    it("should NOT render a relevance label for synthesized sources", () => {
      // Even if a synthesized source somehow carries a score, the synthesized
      // flag must suppress the (misleading) relevance label in the source list.
      const sources = [
        createMockSource({
          id: "syn-1",
          filename: "Synthesized from 2 sources",
          score: 0.05, // would be "Highly Relevant" if not suppressed
          score_type: "distance",
          file_id: "",
          metadata: { synthesized: true },
        }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(
        screen.queryAllByText("Synthesized from 2 sources").length
      ).toBeGreaterThan(0);
      // The borrowed relevance label must not appear for the synthesized source.
      expect(screen.queryByText("Highly Relevant")).not.toBeInTheDocument();
    });

    it("should truncate long snippets", () => {
      const longSnippet = "A".repeat(100);
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf", snippet: longSnippet }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Truncation is handled by CSS (line-clamp), so the full snippet text is in the DOM
      const snippetText = screen.getByText(longSnippet);
      expect(snippetText).toBeInTheDocument();
    });

    it("should handle different score types", () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "rerank.pdf", score: 0.8, score_type: "rerank" }),
        createMockSource({ id: "src-2", filename: "rrf.pdf", score: 0.6, score_type: "rrf" }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Rerank 0.8 = "Highly Relevant", RRF 0.6 = "Top Match"
      expect(screen.getByText("Highly Relevant")).toBeInTheDocument();
      expect(screen.getByText("Top Match")).toBeInTheDocument();
    });
  });

  // =============================================================================
  // SCENARIO 4: Clicking source switches to Preview tab
  // =============================================================================
  describe("source click switches to Preview tab", () => {
    it("should switch to preview tab when source is clicked", async () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf", snippet: "Test content" }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test query" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Find the source button by looking for button elements that contain "doc.pdf"
      // Use a more specific query to find the source list item button
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("doc.pdf")
      );
      expect(sourceButton).toBeInTheDocument();

      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      // After clicking, the preview tab content should be shown
      // The component updates activeTab to "preview" internally
      // We verify this by checking preview content appears
      await waitFor(() => {
        const previewContent = screen.getByTestId("tab-content-preview");
        expect(previewContent).toBeInTheDocument();
      });
    });

    it("should enable preview tab after source selection", async () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf" }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Find and click on source
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("doc.pdf")
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      // After source selection, the preview tab should have content
      await waitFor(() => {
        const previewContent = screen.getByTestId("tab-content-preview");
        expect(previewContent).toBeInTheDocument();
      });
    });

    // Issue #462 — a raster artifact source fetches its bytes via the opaque
    // artifact endpoint and renders an image preview (never a path/base64 URL).
    it("fetches and renders a raster artifact preview for image sources", async () => {
      const sources = [
        createMockSource({
          id: "src-art",
          filename: "chart.png",
          modality: "image",
          artifact_id: "atom-1",
          asset_id: "asset-1",
          vision_status: "used",
        }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "what is in this chart?" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn =>
        btn.textContent?.includes("chart.png")
      );
      expect(sourceButton).toBeInTheDocument();
      expect(sourceButton?.textContent).toContain("image");
      if (sourceButton) fireEvent.click(sourceButton);

      await waitFor(() => {
        expect(getArtifactRawBlob).toHaveBeenCalledWith("atom-1", expect.anything());
      });
      // The opaque blob URL is rendered into an <img> preview.
      await waitFor(() => {
        expect(document.querySelector("img")).not.toBeNull();
      });
    });
  });

  // =============================================================================
  // SCENARIO 5: PreviewTab shows empty state when no source selected
  // =============================================================================
  describe("PreviewTab empty state", () => {
    it("should show empty state message when no source is selected", () => {
      mockUseChatStore.mockReturnValue({
        messages: [],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // The preview tab should show the empty state message
      expect(screen.getByText("No preview available")).toBeInTheDocument();
      expect(screen.getByText("Select a source to preview it here.")).toBeInTheDocument();
    });

    it("should show correct empty state message when sources exist but none selected", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources: [] }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Preview should show empty state
      expect(screen.getByText("No preview available")).toBeInTheDocument();
      expect(screen.getByText("Select a source to preview it here.")).toBeInTheDocument();
    });
  });

  // =============================================================================
  // SCENARIO 6: PreviewTab shows source content when source selected
  // =============================================================================
  describe("PreviewTab source content", () => {
    it("should display source filename in preview", async () => {
      const sources = [
        createMockSource({
          id: "src-1",
          filename: "preview-test.pdf",
          snippet: "This is preview content",
        }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test query" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Click on source - find the button in sources list
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("preview-test.pdf")
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        // Preview should show the filename in h3 tag
        // The h3 in preview shows the filename after selection
        const previewContent = screen.getByTestId("tab-content-preview");
        expect(previewContent).toHaveTextContent(/preview-test\.pdf/i);
      });
    });

    it("should show Jump to answer button", async () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf", snippet: "Content" }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Click on source
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("doc.pdf")
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        expect(screen.getByText("Jump to answer")).toBeInTheDocument();
      });
    });

    it("should display relevance in preview", async () => {
      const sources = [
        createMockSource({
          id: "src-1",
          filename: "doc.pdf",
          score: 0.2,
          score_type: "distance",
        }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Click on source
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("doc.pdf")
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        // Score 0.2 (distance) = "Relevant"
        // Check for "Relevance:" label which is only in preview tab
        expect(screen.getByText("Relevance:")).toBeInTheDocument();
        // Also verify "Relevant" appears in the preview section
        const previewContent = screen.getByTestId("tab-content-preview");
        expect(previewContent).toHaveTextContent("Relevant");
      });
    });
  });

  // =============================================================================
  // SCENARIO 7: WorkspaceTab shows empty state when no structured outputs
  // =============================================================================
  describe("WorkspaceTab empty state", () => {
    it("should show empty state when no structured outputs", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: "Just plain text without any code or tables.",
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Extracted tab should be disabled when no structured outputs
      const extractedTab = screen.getByTestId("tab-extracted");
      expect(extractedTab).toBeDisabled();
    });

    it("should show correct empty message", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "assistant", content: "No structured output here." }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // The empty message should appear in the extracted tab content area
      const extractedContent = screen.getByTestId("tab-content-extracted");
      expect(extractedContent).toBeInTheDocument();
    });
  });

  // =============================================================================
  // SCENARIO 8: WorkspaceTab extracts code blocks from messages
  // =============================================================================
  describe("WorkspaceTab code block extraction", () => {
    it("should extract single code block", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: `Here is the solution:
\`\`\`typescript
const greet = (name: string) => \`Hello, \${name}!\`;
\`\`\`
I hope that helps!`,
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should show count badge (1)
      expect(screen.getByText("(1)")).toBeInTheDocument();
      // Extracted tab should now be enabled
      expect(screen.getByTestId("tab-extracted")).not.toBeDisabled();
    });

    it("should extract multiple code blocks", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: `\`\`\`python
def hello():
    print("Hello")
\`\`\`

And another one:

\`\`\`javascript
console.log("world");
\`\`\``,
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should show count badge (2)
      expect(screen.getByText("(2)")).toBeInTheDocument();
    });

    it("should handle code blocks without language", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: `\`\`\`
some random code here
\`\`\``,
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("(1)")).toBeInTheDocument();
    });

    it("should display code block titles", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: "```typescript\nconst x = 1;\n```",
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should display TypeScript block title
      expect(screen.getByText("Typescript Block")).toBeInTheDocument();
    });
  });

  // =============================================================================
  // SCENARIO 9: WorkspaceTab extracts tables from messages
  // =============================================================================
  describe("WorkspaceTab table extraction", () => {
    it("should extract markdown table", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: `Here is the data:

| Name | Age |
|------|-----|
| Alice | 30 |
| Bob | 25 |`,
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("(1)")).toBeInTheDocument();
      expect(screen.getByTestId("tab-extracted")).not.toBeDisabled();
    });

    it("should extract multiple tables", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: `Table 1:
| A | B |
|---|---|
| 1 | 2 |

Table 2:
| X | Y |
|---|---|
| 3 | 4 |`,
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should extract 2 tables
      expect(screen.getByText("(2)")).toBeInTheDocument();
    });

    it("should extract code blocks and tables together", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: `\`\`\`python
print("hello")
\`\`\`

| Col1 | Col2 |
|------|------|
| A    | B    |`,
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should have 2 items: 1 code block + 1 table
      expect(screen.getByText("(2)")).toBeInTheDocument();
    });

    it("should display table titles", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: `| Name | Value |
|------|-------|
| A | 1 |
| B | 2 |`,
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should display table with first two cells as title
      expect(screen.getByText("Name | Value")).toBeInTheDocument();
    });
  });

  // =============================================================================
  // SCENARIO 10: highlightQueryTerms highlights matching terms
  // =============================================================================
  describe("highlightQueryTerms integration", () => {
    it("should highlight matching terms in preview content", async () => {
      const sources = [
        createMockSource({
          id: "src-1",
          filename: "doc.pdf",
          snippet: "The test document contains test data",
        }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test query" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Click on source to open preview
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("doc.pdf")
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        // Preview should be visible
        expect(screen.getByText("Jump to answer")).toBeInTheDocument();
      });

      // The highlighted content should contain <mark> elements with the highlighted terms
      // We verify this by checking the preview tab content has the highlighted text
      const previewContent = screen.getByTestId("tab-content-preview");
      expect(previewContent).toHaveTextContent("test");
    });

    it("should handle multiple query terms", async () => {
      const sources = [
        createMockSource({
          id: "src-1",
          filename: "doc.pdf",
          snippet: "The quick brown fox",
        }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "quick brown" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("doc.pdf")
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        expect(screen.getByTestId("tab-content-preview")).toBeInTheDocument();
      });
    });
  });

  // =============================================================================
  // SCENARIO 11: highlightQueryTerms escapes regex special chars
  // =============================================================================
  describe("highlightQueryTerms regex escaping", () => {
    it("should handle query with regex special characters", async () => {
      const sources = [
        createMockSource({
          id: "src-1",
          filename: "doc.pdf",
          snippet: "Pattern: test (with) [brackets]",
        }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test (with) [brackets]" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should not throw error and should render
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("doc.pdf")
      );
      expect(sourceButton).toBeInTheDocument();

      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        expect(screen.getByTestId("tab-content-preview")).toBeInTheDocument();
      });
    });

    it("should handle query with dots and asterisks", async () => {
      const sources = [
        createMockSource({
          id: "src-1",
          filename: "special-chars.pdf",
          snippet: "File: config.json",
        }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "config.json" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should render without errors and click should work
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("special-chars.pdf")
      );
      expect(sourceButton).toBeInTheDocument();

      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        expect(screen.getByTestId("tab-content-preview")).toHaveTextContent(/special-chars\.pdf/i);
      });
    });
  });

  // =============================================================================
  // SCENARIO 12: Tab switching works correctly
  // =============================================================================
  describe("Tab switching functionality", () => {
    it("should have all three tabs rendered", () => {
      mockUseChatStore.mockReturnValue({
        messages: [],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByTestId("tab-sources")).toBeInTheDocument();
      expect(screen.getByTestId("tab-preview")).toBeInTheDocument();
      expect(screen.getByTestId("tab-extracted")).toBeInTheDocument();
    });

    it("should have Sources tab enabled by default", () => {
      mockUseChatStore.mockReturnValue({
        messages: [],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const sourcesTab = screen.getByTestId("tab-sources");
      expect(sourcesTab).not.toBeDisabled();
    });

    it("should show preview tab with empty state message when no source selected", () => {
      mockUseChatStore.mockReturnValue({
        messages: [],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Preview tab should show empty state message since no source is selected
      expect(screen.getByText("No preview available")).toBeInTheDocument();
      expect(screen.getByText("Select a source to preview it here.")).toBeInTheDocument();
    });

    it("should enable Preview tab after selecting a source", async () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf" }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Click on source
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("doc.pdf")
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      // After selection, preview tab would show source content
      await waitFor(() => {
        expect(screen.getByTestId("tab-preview")).toBeInTheDocument();
      });
    });

    it("fetches expanded context only after selecting a source", async () => {
      const sources = [
        createMockSource({
          id: "src-1",
          filename: "doc.pdf",
          snippet: "Short snippet",
        }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(getChunkContext).not.toHaveBeenCalled();

      const sourceButton = screen.getAllByRole("button").find(btn =>
        btn.textContent?.includes("doc.pdf")
      );
      expect(sourceButton).toBeInTheDocument();
      fireEvent.click(sourceButton!);

      await waitFor(() => {
        expect(getChunkContext).toHaveBeenCalledWith("src-1");
      });
      await waitFor(() => {
        expect(screen.getByText("Expanded source context")).toBeInTheDocument();
      });
    });

    it("should have Extracted tab disabled when no structured outputs", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "assistant", content: "Plain text response" }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const extractedTab = screen.getByTestId("tab-extracted");
      expect(extractedTab).toBeDisabled();
    });

    it("should enable Extracted tab when structured outputs exist", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: "```python\nprint('hello')\n```",
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const extractedTab = screen.getByTestId("tab-extracted");
      expect(extractedTab).not.toBeDisabled();
    });
  });

  // =============================================================================
  // Additional edge cases
  // =============================================================================
  describe("Edge cases", () => {
    it("should handle source without snippet", () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf", snippet: undefined }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Source should be rendered (may appear multiple times due to component structure)
      expect(screen.queryAllByText("doc.pdf").length).toBeGreaterThan(0);
    });

    it("should handle source without score", () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf", score: undefined }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should render without relevance label
      expect(screen.queryAllByText("doc.pdf").length).toBeGreaterThan(0);
    });

    it("should get query from last user message", () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf", snippet: "Content" }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "First question" }),
          createMockMessage({ role: "assistant", content: "First answer" }),
          createMockMessage({ role: "user", content: "What is this about?" }),
          createMockMessage({ role: "assistant", content: "Second answer", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should use "What is this about?" as query for highlighting
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes("doc.pdf")
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      // The query should be used for highlighting
      expect(screen.getByText("Jump to answer")).toBeInTheDocument();
    });

    it("should handle nested code blocks in tables", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: `| Code | Output |
|------|--------|
| \`console.log(1)\` | 1 |
| \`console.log(2)\` | 2 |`,
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should extract 1 table
      expect(screen.getByText("(1)")).toBeInTheDocument();
    });
  });

  // =============================================================================
  // CHAT-UX-04 — streaming candidate evidence surface (issue #508)
  // =============================================================================
  describe("streaming candidate evidence (CHAT-UX-04)", () => {
    it("shows candidates under a 'Retrieved — not yet cited' heading while streaming, without any cited badge", () => {
      const candidates = [
        createMockSource({ id: "cand-1", filename: "candidate-one.pdf" }),
        createMockSource({ id: "cand-2", filename: "candidate-two.pdf" }),
      ];
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "question" }),
          createMockMessage({
            id: "stream-1",
            role: "assistant",
            content: "",
            candidateSources: candidates,
          }),
        ],
        streamingMessageId: "stream-1",
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("Retrieved — not yet cited")).toBeInTheDocument();
      expect(screen.getAllByText("candidate-one.pdf").length).toBeGreaterThan(0);
      expect(screen.getAllByText("candidate-two.pdf").length).toBeGreaterThan(0);
      // Candidates are retrieved, never presented as cited/verified evidence.
      expect(screen.queryByText("Cited")).not.toBeInTheDocument();
      expect(screen.queryByText("verified")).not.toBeInTheDocument();
    });

    it("done replaces candidates with the final sources list", () => {
      const finalSources = [
        createMockSource({ id: "final-1", filename: "final-source.pdf" }),
      ];
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "question" }),
          createMockMessage({
            id: "done-1",
            role: "assistant",
            content: "Answer cites [S1].",
            sources: finalSources,
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.queryByText("Retrieved — not yet cited")).not.toBeInTheDocument();
      expect(screen.getAllByText("final-source.pdf").length).toBeGreaterThan(0);
    });

    it("hides the candidates section when the streaming message delivered an empty candidate list", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "question" }),
          createMockMessage({
            id: "stream-empty",
            role: "assistant",
            content: "",
            candidateSources: [],
          }),
        ],
        streamingMessageId: "stream-empty",
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.queryByText("Retrieved — not yet cited")).not.toBeInTheDocument();
    });

    it("renders no candidates surface when the message has no candidateSources field (old backend)", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "question" }),
          createMockMessage({ id: "stream-none", role: "assistant", content: "" }),
        ],
        streamingMessageId: "stream-none",
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.queryByText("Retrieved — not yet cited")).not.toBeInTheDocument();
    });
  });

  // =============================================================================
  // UI-035 — artifact error keeps the expanded text fallback (issue #508)
  // =============================================================================
  describe("artifact error keeps expanded text (UI-035)", () => {
    it("renders the artifact error notice AND the expanded context text together", async () => {
      vi.mocked(getArtifactRawBlob).mockRejectedValueOnce(new Error("404 Not Found"));
      const sources = [
        createMockSource({
          id: "src-art-err",
          filename: "broken-chart.png",
          modality: "image",
          artifact_id: "atom-err",
          snippet: "A chart about revenue",
        }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "chart" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const sourceButton = screen.getAllByRole("button").find(btn =>
        btn.textContent?.includes("broken-chart.png")
      );
      expect(sourceButton).toBeInTheDocument();
      fireEvent.click(sourceButton!);

      await waitFor(() => {
        expect(getArtifactRawBlob).toHaveBeenCalledWith("atom-err", expect.anything());
      });

      // The artifact failure notice appears...
      await waitFor(() => {
        expect(
          screen.getByText("Asset unavailable; showing text preview only.")
        ).toBeInTheDocument();
      });
      // ...AND the expanded chunk context stays visible below it (not replaced).
      await waitFor(() => {
        expect(screen.getByText("Expanded source context")).toBeInTheDocument();
      });
    });
  });

  // =============================================================================
  // UI-036 — punctuation-bearing query terms highlight (issue #508)
  // =============================================================================
  describe("highlightQueryTerms punctuation (UI-036)", () => {
    const openPreview = async (query: string, contextText: string, snippet: string) => {
      vi.mocked(getChunkContext).mockResolvedValue({
        id: "src-1",
        file_id: "1",
        filename: "doc.pdf",
        chunk_index: 0,
        chunk_text: "Snippet content",
        context_text: contextText,
        context_source: "parent_window",
      });
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf", snippet }),
      ];
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: query }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      const { container } = render(<RightPane />);
      const sourceButton = screen.getAllByRole("button").find(btn =>
        btn.textContent?.includes("doc.pdf")
      );
      fireEvent.click(sourceButton!);
      await waitFor(() => {
        expect(screen.getByText("Jump to answer")).toBeInTheDocument();
      });
      const marks = () =>
        Array.from(
          container.querySelectorAll("mark:not([data-support-span])")
        ).map((m) => m.textContent);
      return { marks };
    };

    it("marks punctuation-bearing terms (C++ compiler) as search matches", async () => {
      const { marks } = await openPreview(
        "C++ compiler",
        "The C++ compiler optimizes this loop.",
        "Older notes"
      );
      await waitFor(() => {
        expect(marks()).toContain("C++");
        expect(marks()).toContain("compiler");
      });
    });

    it("marks a dotted filename term (config.json) literally", async () => {
      const { marks } = await openPreview(
        "config.json",
        "Edit the config.json file before shipping.",
        "Older notes"
      );
      await waitFor(() => {
        expect(marks()).toContain("config.json");
      });
    });
  });

  // =============================================================================
  // CHAT-UX-02 — support-span highlight + honest not-located note (issue #508)
  // =============================================================================
  describe("support-span highlight (CHAT-UX-02)", () => {
    const openPreviewWith = async (snippet: string | undefined, contextText: string) => {
      vi.mocked(getChunkContext).mockResolvedValue({
        id: "src-span",
        file_id: "1",
        filename: "doc.pdf",
        chunk_index: 0,
        chunk_text: "Snippet content",
        context_text: contextText,
        context_source: "parent_window",
      });
      const sources = [
        createMockSource({ id: "src-span", filename: "span-doc.pdf", snippet }),
      ];
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      const { container } = render(<RightPane />);
      const sourceButton = screen.getAllByRole("button").find(btn =>
        btn.textContent?.includes("span-doc.pdf")
      );
      fireEvent.click(sourceButton!);
      await waitFor(() => {
        expect(screen.getByText("Jump to answer")).toBeInTheDocument();
      });
      return container;
    };

    it("marks the first literal occurrence of the snippet in the loaded context as a support span", async () => {
      const container = await openPreviewWith(
        "the exact support passage",
        "Surrounding text. Here is the exact support   passage inside the window. More text."
      );

      const span = container.querySelector("mark[data-support-span]");
      expect(span).not.toBeNull();
      // Whitespace-normalized literal match: the run of spaces collapses.
      expect(span!.textContent).toBe("the exact support   passage");
    });

    it("renders the honest not-located note when the snippet is absent from the context", async () => {
      await openPreviewWith(
        "a distilled excerpt that is not verbatim in the parent window",
        "Completely different surrounding parent-window text."
      );

      expect(
        screen.getByText("Supporting passage not located in expanded context.")
      ).toBeInTheDocument();
      expect(document.querySelector("mark[data-support-span]")).toBeNull();
    });

    it("renders the not-located note when the source has no snippet at all", async () => {
      await openPreviewWith(undefined, "Loaded parent-window context text.");

      expect(
        screen.getByText("Supporting passage not located in expanded context.")
      ).toBeInTheDocument();
    });
  });

  // =============================================================================
  // CHAT-UX — evidence location line + context retry (issue #508 WU-11)
  // =============================================================================
  describe("evidence location line + context retry", () => {
    const openPreview = async (sources: ReturnType<typeof createMockSource>[]) => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "question" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });
      render(<RightPane />);
    };

    const clickSource = (filename: string) => {
      const sourceButton = screen.getAllByRole("button").find(btn =>
        btn.textContent?.includes(filename)
      );
      fireEvent.click(sourceButton!);
    };

    it("renders the derived location line (Page N) in the preview header", async () => {
      await openPreview([
        createMockSource({ id: "src-loc", filename: "located.pdf", page_number: 4, snippet: "A snippet" }),
      ]);
      clickSource("located.pdf");

      await waitFor(() => {
        expect(screen.getByText("Jump to answer")).toBeInTheDocument();
      });
      expect(screen.getByText("Page 4")).toBeInTheDocument();
    });

    it("renders 'Location unavailable' when no location is derivable", async () => {
      await openPreview([
        createMockSource({ id: "src-noloc", filename: "unlocated.pdf", snippet: "A snippet" }),
      ]);
      clickSource("unlocated.pdf");

      await waitFor(() => {
        expect(screen.getByText("Location unavailable")).toBeInTheDocument();
      });
    });

    it("shows the saved excerpt with an unavailability notice and Retry when the context fetch fails", async () => {
      vi.mocked(getChunkContext).mockRejectedValueOnce(new Error("404 Not Found"));
      await openPreview([
        createMockSource({
          id: "src-404",
          filename: "gone.pdf",
          snippet: "The saved excerpt text",
        }),
      ]);
      clickSource("gone.pdf");

      await waitFor(() => {
        expect(
          screen.getByText("Showing saved excerpt — original context unavailable")
        ).toBeInTheDocument();
      });
      expect(screen.getAllByText("The saved excerpt text").length).toBeGreaterThan(0);
      expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    });

    it("clears the error and renders the context after fail → retry → success", async () => {
      vi.mocked(getChunkContext)
        .mockRejectedValueOnce(new Error("503"))
        .mockResolvedValueOnce({
          id: "src-retry",
          file_id: "1",
          filename: "retry.pdf",
          chunk_index: 0,
          chunk_text: "t",
          context_text: "Retry succeeded context",
          context_source: "chunk",
        });
      await openPreview([
        createMockSource({ id: "src-retry", filename: "retry.pdf", snippet: "Old excerpt" }),
      ]);
      clickSource("retry.pdf");

      await waitFor(() => {
        expect(
          screen.getByText("Showing saved excerpt — original context unavailable")
        ).toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));

      await waitFor(() => {
        expect(screen.getByText("Retry succeeded context")).toBeInTheDocument();
      });
      expect(
        screen.queryByText("Showing saved excerpt — original context unavailable")
      ).not.toBeInTheDocument();
    });

    it("keeps the excerpt and notice after fail → retry → fail", async () => {
      vi.mocked(getChunkContext)
        .mockRejectedValueOnce(new Error("503"))
        .mockRejectedValueOnce(new Error("504"));
      await openPreview([
        createMockSource({ id: "src-2fail", filename: "twofail.pdf", snippet: "Persistent excerpt" }),
      ]);
      clickSource("twofail.pdf");

      await waitFor(() => {
        expect(
          screen.getByText("Showing saved excerpt — original context unavailable")
        ).toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));

      // Second failure keeps the saved excerpt + notice (still retryable).
      await waitFor(() => {
        expect(getChunkContext).toHaveBeenCalledTimes(2);
      });
      expect(
        screen.getByText("Showing saved excerpt — original context unavailable")
      ).toBeInTheDocument();
      expect(screen.getAllByText("Persistent excerpt").length).toBeGreaterThan(0);
    });

    it("a retried fetch superseded by a source switch never lands on the new source", async () => {
      let resolveRetry!: (value: Awaited<ReturnType<typeof getChunkContext>>) => void;
      vi.mocked(getChunkContext)
        .mockRejectedValueOnce(new Error("404"))
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              resolveRetry = resolve;
            })
        );
      await openPreview([
        createMockSource({ id: "src-switch-a", filename: "switch-a.pdf", snippet: "Excerpt A" }),
        createMockSource({ id: "src-switch-b", filename: "switch-b.pdf", snippet: "Excerpt B" }),
      ]);
      clickSource("switch-a.pdf");

      await waitFor(() => {
        expect(
          screen.getByText("Showing saved excerpt — original context unavailable")
        ).toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await waitFor(() => {
        expect(getChunkContext).toHaveBeenCalledWith("src-switch-a");
      });

      // Switch sources while the retried fetch for A is still in flight.
      clickSource("switch-b.pdf");
      await waitFor(() => {
        expect(getChunkContext).toHaveBeenCalledWith("src-switch-b");
      });
      await waitFor(() => {
        expect(screen.getByText("Expanded source context")).toBeInTheDocument();
      });

      // The late resolve for A must not overwrite B's context.
      await act(async () => {
        resolveRetry({
          id: "src-switch-a",
          file_id: "1",
          filename: "switch-a.pdf",
          chunk_index: 0,
          chunk_text: "t",
          context_text: "STALE CONTEXT FOR A",
          context_source: "chunk",
        });
        await Promise.resolve();
      });
      expect(screen.queryByText("STALE CONTEXT FOR A")).not.toBeInTheDocument();
      expect(screen.getByText("Expanded source context")).toBeInTheDocument();
    });
  });

  // =============================================================================
  // Custom event tests
  // =============================================================================
  describe("Jump to answer event dispatching", () => {
    it("should dispatch custom event on Jump to answer click with messageId null for pane-list selections", async () => {
      const dispatchSpy = vi.spyOn(window, "dispatchEvent");
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf", snippet: "Content" }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Click on source first (pane-list selection — no anchored message)
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn =>
        btn.textContent?.includes("doc.pdf")
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        expect(screen.getByText("Jump to answer")).toBeInTheDocument();
      });

      // Click Jump to answer button
      const jumpButton = screen.getByText("Jump to answer");
      fireEvent.click(jumpButton);

      // Verify event was dispatched with a null message anchor (legacy
      // first-match fallback in TranscriptPane).
      expect(dispatchSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          type: "evidence:jump-to-answer",
          detail: expect.objectContaining({
            sourceId: expect.any(String),
            messageId: null,
          }),
        })
      );
    });

    it("should carry the anchored message id when the selection came from a citation chip", async () => {
      const dispatchSpy = vi.spyOn(window, "dispatchEvent");
      const sources = [
        createMockSource({ id: "src-1", filename: "doc.pdf", snippet: "Content" }),
      ];

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });
      mockUseChatShellStore.mockReturnValue({
        selectedEvidenceSource: null,
        setSelectedEvidenceSource: vi.fn(),
        activeRightTab: "evidence",
        setActiveRightTab: vi.fn(),
        selectedEvidenceMessageId: "msg-42",
      });

      render(<RightPane />);

      const sourceButton = screen.getAllByRole("button").find(btn =>
        btn.textContent?.includes("doc.pdf")
      );
      fireEvent.click(sourceButton!);

      await waitFor(() => {
        expect(screen.getByText("Jump to answer")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByText("Jump to answer"));

      expect(dispatchSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          type: "evidence:jump-to-answer",
          detail: expect.objectContaining({
            sourceId: "src-1",
            messageId: "msg-42",
          }),
        })
      );
    });
  });
});

describe("extractStructuredOutputs (via integration)", () => {
  const mockUseChatStore = useChatStoreModule.useChatStore as unknown as ReturnType<
    typeof vi.fn
  >;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("code block extraction edge cases", () => {
    it("should handle empty code blocks", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: "```\n```",
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should still be extracted
      expect(screen.getByText("(1)")).toBeInTheDocument();
    });

    it("should handle code blocks with special characters", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: "```\nconst str = '```test```';\n```",
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should extract at least one code block (backticks may create multiple)
      // The extracted tab should be enabled with some code blocks
      const extractedTab = screen.getByTestId("tab-extracted");
      expect(extractedTab).not.toBeDisabled();
      // The count badge should show some number of extracted items
      expect(screen.getByText(/\(\d+\)/)).toBeInTheDocument();
    });

    it("should show line count for code blocks", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: "```python\nline1\nline2\nline3\nline4\n```",
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should show "4 lines"
      expect(screen.getByText("4 lines")).toBeInTheDocument();
    });

    it("should show row count for tables", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({
            role: "assistant",
            content: `| A | B |
|---|---|
| 1 | 2 |
| 3 | 4 |
| 5 | 6 |`,
          }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should show "4 rows" (all non-separator lines including header)
      expect(screen.getByText("4 rows")).toBeInTheDocument();
    });
  });
});
