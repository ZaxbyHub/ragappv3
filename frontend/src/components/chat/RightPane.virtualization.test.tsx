/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { RightPane } from "./RightPane";
import * as useChatStoreModule from "@/stores/useChatStore";
import * as useChatShellStoreModule from "@/stores/useChatShellStore";

const apiMocks = vi.hoisted(() => ({
  getChunkContext: vi.fn(),
  getDocumentRawBlob: vi.fn(),
}));

// Mock @tanstack/react-virtual
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(() => ({
    getVirtualItems: vi.fn(() => [
      { index: 0, start: 0, size: 80, key: "source-0", measureElement: vi.fn() },
      { index: 1, start: 80, size: 80, key: "source-1", measureElement: vi.fn() },
    ]),
    getTotalSize: vi.fn(() => 160),
    measureElement: vi.fn(),
  })),
}));

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
  return {
    useChatStore: chatStoreMock,
    useChatMessages: useChatMessagesMock,
    useLastCompletedAssistantSources: useLastCompletedAssistantSourcesMock,
    useLastCompletedAssistantWikiRefs: useLastCompletedAssistantWikiRefsMock,
    useLastCompletedAssistantKmsRefs: useLastCompletedAssistantKmsRefsMock,
    useLastUserContent: useLastUserContentMock,
    useSourcesForSourceId: useSourcesForSourceIdMock,
    useCompletedAssistantMessageIdsKey: useCompletedAssistantMessageIdsKeyMock,
    parseCompletedAssistantIds: (key: string) => {
      if (!key) return [];
      try { const p = JSON.parse(key); return Array.isArray(p) ? p.map(String) : []; } catch { return []; }
    },
  };
});

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getChunkContext: (...args: unknown[]) => apiMocks.getChunkContext(...args),
  getDocumentRawBlob: (...args: unknown[]) => apiMocks.getDocumentRawBlob(...args),
}));

// Mock UI components with proper interactivity
const mockOnValueChange = vi.fn();

vi.mock("@/components/ui/tabs", () => ({
  Tabs: ({ children, value, onValueChange }: any) => {
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
  TabsTrigger: ({ children, value, disabled, onClick }: any) => (
    <button
      data-testid={`tab-${value}`}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  ),
  TabsContent: ({ children, value }: any) => (
    <div data-testid={`tab-content-${value}`}>{children}</div>
  ),
}));

vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children }: any) => <div data-testid="scroll-area">{children}</div>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, onClick, variant, size, asChild, ...props }: any) => {
    if (asChild) {
      return children;
    }
    return (
      <button data-testid={props["data-testid"]} onClick={onClick} {...props}>
        {children}
      </button>
    );
  },
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

// Helper to create many sources
const createManySources = (count: number, startIndex = 0) =>
  Array.from({ length: count }, (_, i) =>
    createMockSource({
      id: `source-${startIndex + i}`,
      filename: `document-${startIndex + i}.pdf`,
      snippet: `Snippet for document ${startIndex + i}`,
      score: 0.1 + (i * 0.01),
      score_type: "distance" as const,
    })
  );

describe("RightPane virtualization", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockOnValueChange.mockClear();
    apiMocks.getChunkContext.mockRejectedValue(new Error("No context"));
    apiMocks.getDocumentRawBlob.mockResolvedValue(
      new Blob(["%PDF-1.4\n"], { type: "application/pdf" })
    );
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);

    // Default store mocks
    mockUseChatStore.mockReturnValue({
      messages: [],
      expandedSources: new Set(),
    });

    mockUseChatShellStore.mockReturnValue({
      selectedEvidenceSource: null,
      setSelectedEvidenceSource: vi.fn(),
      activeRightTab: "evidence",
      setActiveRightTab: vi.fn(),
    });
  });

  // =============================================================================
  // BOUNDARY TESTS: 20 sources (non-virtualized) vs 21 sources (virtualized)
  // =============================================================================
  describe("virtualization boundary at 20/21 sources", () => {
    it("should NOT use virtualization with exactly 20 sources", () => {
      const sources = createManySources(20);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should show ScrollArea within sources tab content (non-virtualized path)
      const sourcesContent = screen.getByTestId("tab-content-sources");
      const scrollArea = sourcesContent.querySelector('[data-testid="scroll-area"]');
      expect(scrollArea).toBeInTheDocument();
    });

    it("should use virtualization with exactly 21 sources", () => {
      const sources = createManySources(21);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should show the virtualized container (overflow-y-auto div with aria-label)
      const virtualizedContainer = screen.getByLabelText("Sources list");
      expect(virtualizedContainer).toBeInTheDocument();
      expect(virtualizedContainer).toHaveClass("overflow-y-auto");
    });
  });

  // =============================================================================
  // EMPTY STATE: 0 sources
  // =============================================================================
  describe("empty state (0 sources)", () => {
    it("should show empty state message when no sources", () => {
      mockUseChatStore.mockReturnValue({
        messages: [],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("No sources yet")).toBeInTheDocument();
      expect(screen.getByText("Send a message to see retrieved sources.")).toBeInTheDocument();
    });

    it("should not render ScrollArea or virtualized container when no sources", () => {
      mockUseChatStore.mockReturnValue({
        messages: [],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const sourcesContent = screen.getByTestId("tab-content-sources");
      expect(sourcesContent.querySelector('[data-testid="scroll-area"]')).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Sources list")).not.toBeInTheDocument();
    });
  });

  // =============================================================================
  // NON-VIRTUALIZED PATH: <= 20 sources
  // =============================================================================
  describe("non-virtualized path (<= 20 sources)", () => {
    it("should render ScrollArea for 1 source", () => {
      const sources = [createMockSource({ id: "src-1", filename: "one.pdf" })];
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const sourcesContent = screen.getByTestId("tab-content-sources");
      const scrollArea = sourcesContent.querySelector('[data-testid="scroll-area"]');
      expect(scrollArea).toBeInTheDocument();
    });

    it("should render ScrollArea for 10 sources", () => {
      const sources = createManySources(10);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const sourcesContent = screen.getByTestId("tab-content-sources");
      const scrollArea = sourcesContent.querySelector('[data-testid="scroll-area"]');
      expect(scrollArea).toBeInTheDocument();
    });

    it("should render ScrollArea for 20 sources", () => {
      const sources = createManySources(20);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const sourcesContent = screen.getByTestId("tab-content-sources");
      const scrollArea = sourcesContent.querySelector('[data-testid="scroll-area"]');
      expect(scrollArea).toBeInTheDocument();
    });

    it("should render all source items in non-virtualized list", () => {
      const sources = [
        createMockSource({ id: "src-1", filename: "first.pdf" }),
        createMockSource({ id: "src-2", filename: "second.pdf" }),
        createMockSource({ id: "src-3", filename: "third.pdf" }),
      ];
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("first.pdf")).toBeInTheDocument();
      expect(screen.getByText("second.pdf")).toBeInTheDocument();
      expect(screen.getByText("third.pdf")).toBeInTheDocument();
    });

    it("should render source count badge correctly for <= 20 sources", () => {
      const sources = createManySources(15);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("(15)")).toBeInTheDocument();
    });
  });

  // =============================================================================
  // VIRTUALIZED PATH: > 20 sources
  // =============================================================================
  describe("virtualized path (> 20 sources)", () => {
    it("should render virtualized container for 21 sources", () => {
      const sources = createManySources(21);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const container = screen.getByLabelText("Sources list");
      expect(container).toBeInTheDocument();
      expect(container).toHaveClass("overflow-y-auto");
    });

    it("should render virtualized container for 50 sources", () => {
      const sources = createManySources(50);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const container = screen.getByLabelText("Sources list");
      expect(container).toBeInTheDocument();
    });

    it("should render virtualized container for 100 sources", () => {
      const sources = createManySources(100);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      const container = screen.getByLabelText("Sources list");
      expect(container).toBeInTheDocument();
    });

    it("should render source count badge correctly for > 20 sources", () => {
      const sources = createManySources(50);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("(50)")).toBeInTheDocument();
    });

    it("should NOT render ScrollArea when virtualized", () => {
      const sources = createManySources(25);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // ScrollArea should NOT be in the sources tab content (it's in the preview tab)
      const sourcesContent = screen.getByTestId("tab-content-sources");
      expect(sourcesContent.querySelector('[data-testid="scroll-area"]')).not.toBeInTheDocument();
    });
  });

  // =============================================================================
  // SOURCE CLICK WORKS IN BOTH PATHS
  // =============================================================================
  describe("source click functionality in both paths", () => {
    it("should handle source click in non-virtualized path (5 sources)", async () => {
      const sources = createManySources(5);
      const setSelectedEvidenceSource = vi.fn();
      const setActiveRightTab = vi.fn();

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      mockUseChatShellStore.mockReturnValue({
        selectedEvidenceSource: null,
        setSelectedEvidenceSource,
        setActiveRightTab,
        activeRightTab: "evidence",
      });

      render(<RightPane />);

      const sourceButton = screen.getByText("document-0.pdf").closest("button");
      expect(sourceButton).toBeInTheDocument();

      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        expect(setSelectedEvidenceSource).toHaveBeenCalledWith(sources[0]);
      });
    });

    it("should handle source click in virtualized path (25 sources)", async () => {
      const sources = createManySources(25);
      const setSelectedEvidenceSource = vi.fn();
      const setActiveRightTab = vi.fn();

      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources }),
        ],
        expandedSources: new Set(),
      });

      mockUseChatShellStore.mockReturnValue({
        selectedEvidenceSource: null,
        setSelectedEvidenceSource,
        setActiveRightTab,
        activeRightTab: "evidence",
      });

      render(<RightPane />);

      // The virtualized list renders items - we look for the source by filename
      // In virtualized mode, items may not all be in DOM but the click handler should work
      const sourceButtons = screen.getAllByText(/document-0\.pdf/);
      // There might be multiple matches if preview is also shown
      const sourceButton = sourceButtons[0]?.closest("button");
      expect(sourceButton).toBeInTheDocument();

      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        expect(setSelectedEvidenceSource).toHaveBeenCalled();
      });
    });
  });

  // =============================================================================
  // EDGE CASES
  // =============================================================================
  describe("edge cases", () => {
    it("should handle exactly 0 sources", () => {
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources: [] }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      expect(screen.getByText("No sources yet")).toBeInTheDocument();
      expect(screen.getByText("Send a message to see retrieved sources.")).toBeInTheDocument();
    });

    it("should handle sources with undefined/null values filtered out", () => {
      const sources = [
        createMockSource({ id: "src-1" }),
        // @ts-expect-error - simulating filtered null
        null,
        createMockSource({ id: "src-3" }),
      ];
      // The component filters null sources, so we pass valid array after filter
      const validSources = sources.filter(Boolean);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources: validSources }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);

      // Should show 2 sources in count
      expect(screen.getByText("(2)")).toBeInTheDocument();
    });

    it("should handle switching from non-virtualized to virtualized count", () => {
      // First render with 5 sources (non-virtualized)
      const sources5 = createManySources(5);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources: sources5 }),
        ],
        expandedSources: new Set(),
      });

      const { rerender } = render(<RightPane />);
      const sourcesContent = screen.getByTestId("tab-content-sources");
      expect(sourcesContent.querySelector('[data-testid="scroll-area"]')).toBeInTheDocument();

      // Rerender with 25 sources (virtualized)
      const sources25 = createManySources(25);
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "test" }),
          createMockMessage({ role: "assistant", content: "response", sources: sources25 }),
        ],
        expandedSources: new Set(),
      });

      rerender(<RightPane />);
      expect(screen.getByLabelText("Sources list")).toBeInTheDocument();
    });
  });

  describe("document preview", () => {
    it("fetches document bytes through the authenticated API and targets the cited PDF page", async () => {
      const source = createMockSource({
        id: "src-pdf",
        file_id: "42",
        filename: "manual.pdf",
        page_number: 5,
      });
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "manual" }),
          createMockMessage({ role: "assistant", content: "response", sources: [source] }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);
      fireEvent.click(screen.getByText("manual.pdf").closest("button")!);
      fireEvent.click(await screen.findByRole("button", { name: /open document/i }));

      await waitFor(() => {
        expect(apiMocks.getDocumentRawBlob).toHaveBeenCalledWith(
          "42",
          expect.any(AbortSignal)
        );
      });
      const frame = await screen.findByTitle("Preview of manual.pdf");
      expect(frame).toHaveAttribute("src", "blob:preview#page=5");
    });

    it("uses metadata page numbers when source page_number is not flattened", async () => {
      const source = createMockSource({
        id: "src-metadata-page",
        file_id: "45",
        filename: "appendix.pdf",
        metadata: { page_number: 9 },
      });
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "appendix" }),
          createMockMessage({ role: "assistant", content: "response", sources: [source] }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);
      fireEvent.click(screen.getByText("appendix.pdf").closest("button")!);
      fireEvent.click(await screen.findByRole("button", { name: /open document/i }));

      const frame = await screen.findByTitle("Preview of appendix.pdf");
      expect(frame).toHaveAttribute("src", "blob:preview#page=9");
    });

    it("falls back to downloading the original for non-PDF sources", async () => {
      apiMocks.getDocumentRawBlob.mockResolvedValueOnce(
        new Blob(["<script>window.opener.location='https://evil.example'</script>"], {
          type: "text/html",
        })
      );
      const source = createMockSource({
        id: "src-html",
        file_id: "43",
        filename: "preview.html",
      });
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "preview" }),
          createMockMessage({ role: "assistant", content: "response", sources: [source] }),
        ],
        expandedSources: new Set(),
      });

      render(<RightPane />);
      fireEvent.click(screen.getByText("preview.html").closest("button")!);
      fireEvent.click(await screen.findByRole("button", { name: /open document/i }));

      expect(await screen.findByText("Preview is available for PDF files.")).toBeInTheDocument();
      const fallbackLink = screen.getByRole("link", { name: /download original/i });
      expect(fallbackLink).toHaveAttribute("href", "blob:preview");
      expect(fallbackLink).toHaveAttribute("download", "preview.html");
      expect(fallbackLink).not.toHaveAttribute("target");
    });

    it("revokes object URLs when the preview unmounts", async () => {
      const source = createMockSource({
        id: "src-cleanup",
        file_id: "44",
        filename: "cleanup.pdf",
      });
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "cleanup" }),
          createMockMessage({ role: "assistant", content: "response", sources: [source] }),
        ],
        expandedSources: new Set(),
      });

      const { unmount } = render(<RightPane />);
      fireEvent.click(screen.getByText("cleanup.pdf").closest("button")!);
      fireEvent.click(await screen.findByRole("button", { name: /open document/i }));

      await screen.findByTitle("Preview of cleanup.pdf");
      unmount();

      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:preview");
    });

    it("does not create a stale object URL when an aborted request resolves", async () => {
      let resolveBlob!: (blob: Blob) => void;
      apiMocks.getDocumentRawBlob.mockReturnValueOnce(
        new Promise<Blob>((resolve) => {
          resolveBlob = resolve;
        })
      );
      const source = createMockSource({
        id: "src-aborted",
        file_id: "46",
        filename: "aborted.pdf",
      });
      mockUseChatStore.mockReturnValue({
        messages: [
          createMockMessage({ role: "user", content: "abort" }),
          createMockMessage({ role: "assistant", content: "response", sources: [source] }),
        ],
        expandedSources: new Set(),
      });

      const { unmount } = render(<RightPane />);
      fireEvent.click(screen.getByText("aborted.pdf").closest("button")!);
      fireEvent.click(await screen.findByRole("button", { name: /open document/i }));

      await waitFor(() => {
        expect(apiMocks.getDocumentRawBlob).toHaveBeenCalledWith(
          "46",
          expect.any(AbortSignal)
        );
      });
      unmount();

      await act(async () => {
        resolveBlob(new Blob(["%PDF-1.4\n"], { type: "application/pdf" }));
        await Promise.resolve();
      });

      expect(URL.createObjectURL).not.toHaveBeenCalled();
    });
  });
});
