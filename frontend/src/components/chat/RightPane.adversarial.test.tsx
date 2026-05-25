// frontend/src/components/chat/RightPane.adversarial.test.tsx
// ADVERSARIAL TESTS: Security, edge cases, and attack vectors for RightPane component

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { RightPane } from "./RightPane";
import { useChatStore } from "@/stores/useChatStore";
import type { Source } from "@/lib/api";

// Mock ResizeObserver for Radix UI ScrollArea
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

// Mock scrollIntoView
Element.prototype.scrollIntoView = vi.fn();

// Mock custom event dispatch
const mockDispatchEvent = vi.fn();
global.dispatchEvent = mockDispatchEvent;

// =============================================================================
// MOCKS
// =============================================================================

vi.mock("@/stores/useChatStore", () => {
  const chatStoreMock: any = vi.fn();
  // RightPane uses useChatStore.getState() to read messagesById when
  // computing structured outputs. Provide a getState shim that exposes
  // a messagesById map derived from whatever fixture the test installs.
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
  // Granular selectors used by the new RightPane. They derive from the
  // same mock state so existing fixtures continue to work.
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
  useChatShellStore: vi.fn(() => ({
    selectedEvidenceSource: null,
    setSelectedEvidenceSource: vi.fn(),
    activeRightTab: "evidence",
    setActiveRightTab: vi.fn(),
  })),
}));

const createMockMessages = (overrides: any[] = []) => ({
  messages: overrides,
  input: "",
  isStreaming: false,
  setInput: vi.fn(),
  setIsStreaming: vi.fn(),
  setAbortFn: vi.fn(),
  setInputError: vi.fn(),
  addMessage: vi.fn(),
  updateMessage: vi.fn(),
  inputError: null,
});

const createSource = (overrides: Partial<Source> = {}): Source => ({
  id: "src-1",
  filename: "test.txt",
  snippet: "Test content",
  score: 0.5,
  score_type: "distance",
  ...overrides,
});

// =============================================================================
// ADVERSARIAL TEST SUITE
// =============================================================================

describe("RightPane ADVERSARIAL TESTS", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockDispatchEvent.mockClear();
    (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      messages: [],
      input: "",
      isStreaming: false,
      setInput: vi.fn(),
      setIsStreaming: vi.fn(),
      setAbortFn: vi.fn(),
      setInputError: vi.fn(),
      addMessage: vi.fn(),
      updateMessage: vi.fn(),
      inputError: null,
    });
  });

  // ===========================================================================
  // 1. XSS IN SOURCES - Should render safely without executing scripts
  // ===========================================================================
  describe("XSS in sources", () => {
    const xssPayloads = [
      '<img onerror="alert(1)" src=x>',
      '<script>alert("xss")</script>',
      '<svg onload="alert(1)">',
      '"><script>alert(document.cookie)</script>',
      '<a href="javascript:alert(1)">Click</a>',
      '<div onclick="alert(1)">click me</div>',
      '${alert(1)}',
      '{{constructor.constructor("alert(1)")()}}',
      '<img src=x onerror="eval(atob(\'YWxlcnQoMSk=\'))">',
      '<script>document.write("<img src=x>")</script>',
      '<style>@import"javascript:alert(1)"</style>',
    ];

    it.each(xssPayloads)("should NOT execute XSS in source filename: %s", async (payload) => {
      const sources = [createSource({ filename: payload })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          {
            id: "msg-1",
            role: "user",
            content: "test query",
          },
          {
            id: "msg-2",
            role: "assistant",
            content: "Here are the results",
            sources,
          },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // No script elements should be created
      expect(document.querySelector("script")).toBeNull();
    });

    it.each(xssPayloads)("should NOT execute XSS in source snippet: %s", async (payload) => {
      const sources = [createSource({ snippet: payload })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          {
            id: "msg-1",
            role: "user",
            content: "test query",
          },
          {
            id: "msg-2",
            role: "assistant",
            content: "Here are the results",
            sources,
          },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      expect(document.querySelector("script")).toBeNull();
    });

    it("should safely render XSS payload in source content when previewing", async () => {
      const xssFilename = '<img onerror="alert(1)" src=x>';
      const sources = [createSource({ id: "xss-src", filename: xssFilename, snippet: "XSS snippet" })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          {
            id: "msg-1",
            role: "user",
            content: "test query",
          },
          {
            id: "msg-2",
            role: "assistant",
            content: "Results",
            sources,
          },
        ])
      );

      render(<RightPane />);

      // Click on source to preview
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes(xssFilename)
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        expect(screen.getByText("Preview")).toBeInTheDocument();
      });

      // No script execution
      expect(document.querySelector("script")).toBeNull();
    });
  });

  // ===========================================================================
  // 2. MALFORMED REGEX IN QUERY HIGHLIGHTING
  // ===========================================================================
  describe("Query with all regex special characters", () => {
    const regexSpecialChars = [".", "*", "+", "?", "^", "$", "{", "}", "(", ")", "|", "[", "]", "\\"];

    it("should handle query with ALL regex special chars without crashing", async () => {
      const query = ".*+?^${}()|[]\\";
      // Use a filename that doesn't contain query terms to avoid highlighting complications
      const sources = [createSource({ 
        filename: "unrelated.txt", 
        snippet: "Some content that doesn't match the query" 
      })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: query },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      // Component should render without crashing
      expect(() => {
        render(<RightPane />);
      }).not.toThrow();

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });
    });

    it.each(regexSpecialChars)("should handle query with single regex char: '%s'", async (char) => {
      const sources = [createSource({ snippet: "test content" })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: char },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle empty query string", async () => {
      const sources = [createSource({ snippet: "test content" })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // Should render without error
      expect(screen.getByText("S1")).toBeInTheDocument();
    });

    it("should handle query with only whitespace", async () => {
      const sources = [createSource({ snippet: "test content" })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "   " },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle potentially catastrophic regex: '.*.*.*.*.*'", async () => {
      const query = ".*.*.*.*.*";
      const sources = [createSource({ snippet: "a".repeat(1000) })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: query },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle regex injection attempt: 'test(?=injection)'", async () => {
      const query = "test(?=injection)";
      const sources = [createSource({ snippet: "test content" })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: query },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });
  });

  // ===========================================================================
  // 3. VERY LONG SOURCE CONTENT (100K+ chars)
  // ===========================================================================
  describe("Very long source content (100K+ chars)", () => {
    it("should handle 100K character snippet without breaking", async () => {
      const longSnippet = "A".repeat(100000);
      const sources = [createSource({ snippet: longSnippet })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // Should still render
      expect(screen.getByText("S1")).toBeInTheDocument();
    });

    it("should handle 500K character snippet", async () => {
      const longSnippet = "Lorem ipsum ".repeat(50000);
      const sources = [createSource({ snippet: longSnippet })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle very long filename (1000+ chars)", async () => {
      const longFilename = "A".repeat(1000) + ".txt";
      const sources = [createSource({ filename: longFilename })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // Filename should be truncated
      expect(screen.getByText("S1")).toBeInTheDocument();
    });

    it("should handle snippet with no spaces (100K chars)", async () => {
      const noSpaceSnippet = "AAAAAAAAAA".repeat(10000);
      const sources = [createSource({ snippet: noSpaceSnippet })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle snippet with many newlines (50K lines)", async () => {
      const manyNewlines = "\n".repeat(50000);
      const sources = [createSource({ snippet: manyNewlines + "content" })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });
  });

  // ===========================================================================
  // 4. EXTRACT STRUCTURED OUTPUTS WITH DEEPLY NESTED CODE BLOCKS
  // ===========================================================================
  describe("extractStructuredOutputs with deeply nested code blocks", () => {
    it("should handle message with deeply nested code blocks", async () => {
      const nestedContent = `
Here is some code:

${"```python\n".repeat(10)}
def nested():
    print("deep")
${"```\n".repeat(10)}

And more content.
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: nestedContent },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // Should render extracted tab
      const extractedTab = screen.getByRole("tab", { name: /Extracted/i });
      expect(extractedTab).toBeInTheDocument();
    });

    it("should handle message with 100 code blocks", async () => {
      const manyCodeBlocks = Array.from({ length: 100 }, (_, i) => 
        `\`\`\`python\n# Code block ${i}\nprint("hello")\n\`\`\``
      ).join("\n\n");
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: manyCodeBlocks },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // Click on Extracted tab to see the outputs
      const extractedTab = screen.getByRole("tab", { name: /Extracted/i });
      fireEvent.click(extractedTab);

      await waitFor(() => {
        expect(screen.getByText("Extracted")).toBeInTheDocument();
      });

      // Should have extracted items (may be truncated)
      expect(screen.getByText("Evidence")).toBeInTheDocument();
    });

    it("should handle code blocks with very long lines (10000 chars)", async () => {
      const longLine = "x".repeat(10000);
      const content = `\`\`\`python\n${longLine}\n\`\`\``;
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: content },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle markdown tables with 1000 rows", async () => {
      const header = "| Column 1 | Column 2 |\n|----------|----------|\n";
      const rows = Array.from({ length: 1000 }, (_, i) => `| Row ${i} | Data ${i} |`).join("\n");
      const content = header + rows;
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: content },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });
    });

    it("should handle malformed code block fences", async () => {
      const malformedContent = `
\`\`\`
unclosed code block
\`\`\`

\`\`\`python
code with no closing
\`\`\`
\`\`\`
double closed
\`\`\`
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: malformedContent },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });
  });

  // ===========================================================================
  // 5. EMPTY/NULL SOURCES ARRAY
  // ===========================================================================
  describe("Empty/null sources array", () => {
    it("should show empty state when sources array is empty", async () => {
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources: [] },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("No sources yet")).toBeInTheDocument();
      });

      expect(screen.getByText("Evidence")).toBeInTheDocument();
    });

    it("should show empty state when sources is null", async () => {
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources: null as any },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("No sources yet")).toBeInTheDocument();
      });
    });

    it("should show empty state when sources is undefined", async () => {
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results" }, // sources undefined
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("No sources yet")).toBeInTheDocument();
      });
    });

    it("should show empty state when no messages exist", async () => {
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("No sources yet")).toBeInTheDocument();
      });
    });

    // BUG DISCOVERED: Component crashes when sources array contains null entries
    // Error: TypeError: Cannot read properties of null (reading 'id')
    // The component maps over sources without guarding against null/undefined entries
    it("BUG: crashes with null entries in sources array - should guard against null sources", async () => {
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources: [null, null] as any },
        ])
      );

      // This test FAILS, revealing a bug in the component
      // The component should filter out null/undefined entries or add a guard
      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });
  });

  // ===========================================================================
  // 6. SOURCE WITH UNDEFINED/NULL FIELDS
  // ===========================================================================
  describe("Source with undefined/null fields", () => {
    it("should handle source with undefined filename", async () => {
      const sources = [{ id: "src-1", snippet: "content" } as Source];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });
    });

    it("should handle source with null snippet", async () => {
      const sources = [{ id: "src-1", filename: "test.txt", snippet: null } as Source];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle source with undefined score", async () => {
      const sources = [{ id: "src-1", filename: "test.txt", snippet: "content" } as Source];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });
    });

    it("should handle source with null score", async () => {
      const sources = [{ id: "src-1", filename: "test.txt", snippet: "content", score: null } as Source];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle source with undefined score_type", async () => {
      const sources = [{ id: "src-1", filename: "test.txt", snippet: "content", score: 0.5 } as Source];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });
    });

    it("should handle source with NaN score", async () => {
      const sources = [{ id: "src-1", filename: "test.txt", snippet: "content", score: NaN } as Source];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle source with Infinity score", async () => {
      const sources = [{ id: "src-1", filename: "test.txt", snippet: "content", score: Infinity } as Source];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle source with negative score", async () => {
      const sources = [{ id: "src-1", filename: "test.txt", snippet: "content", score: -0.5 } as Source];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });
    });

    it("should handle source with invalid score_type", async () => {
      const sources = [{ id: "src-1", filename: "test.txt", snippet: "content", score: 0.5, score_type: "invalid" as any }];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });
  });

  // ===========================================================================
  // 7. CODE BLOCK INJECTION VIA MESSAGE CONTENT
  // ===========================================================================
  describe("Code block injection via message content", () => {
    it("should handle code block with HTML injection attempt", async () => {
      const content = `
Check this code:
\`\`\`html
<script>alert("xss")</script>
<img src=x onerror="alert(1)">
\`\`\`
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // No script execution in extracted content
      expect(document.querySelector("script")).toBeNull();
    });

    it("should handle code block with template literal injection", async () => {
      const content = `
\`\`\`javascript
const xss = '\${alert(1)}';
const template = \`\${document.cookie}\`;
\`\`\`
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      expect(document.querySelector("script")).toBeNull();
    });

    it("should handle deeply nested code blocks with injection", async () => {
      const content = `
\`\`\`
\`\`\`
\`\`\`
<script>alert(1)</script>
\`\`\`
\`\`\`
\`\`\`
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle code block with SQL injection attempt", async () => {
      const content = `
\`\`\`sql
SELECT * FROM users WHERE id = 1; DROP TABLE users;--
\`\`\`
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // Should render as extracted content
      const extractedTab = screen.getByRole("tab", { name: /Extracted/i });
      fireEvent.click(extractedTab);

      await waitFor(() => {
        expect(screen.getByText("Extracted")).toBeInTheDocument();
      });
    });

    it("should handle code block with path traversal attempt", async () => {
      const content = `
\`\`\`
../../../etc/passwd
C:\\\\..\\\\..\\\\windows\\\\system32
\`\`\`
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle code block with null bytes", async () => {
      const content = `
\`\`\`
Line 1\\x00with null byte
Line 2\\x00another
\`\`\`
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle code block with RTL Unicode override", async () => {
      const content = `
\`\`\`
\u202EEvil\u202C text injection
Normal text
\`\`\`
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });
    });
  });

  // ===========================================================================
  // 8. JUMP TO ANSWER EVENT HANDLING
  // ===========================================================================
  describe("Jump to answer event handling", () => {
    it("should dispatch custom event when jump to answer clicked", async () => {
      const sources = [createSource({ id: "source-123" })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      // Click on source to select it
      const sourceButtons = screen.getAllByRole("button");
      const sourceButton = sourceButtons.find(btn => 
        btn.textContent?.includes(sources[0].filename)
      );
      if (sourceButton) {
        fireEvent.click(sourceButton);
      }

      await waitFor(() => {
        expect(screen.getByText("Preview")).toBeInTheDocument();
      });

      // Click jump to answer
      const jumpButton = screen.getByText("Jump to answer");
      fireEvent.click(jumpButton);

      // Should dispatch custom event with correct detail
      expect(mockDispatchEvent).toHaveBeenCalledTimes(1);
      const call = mockDispatchEvent.mock.calls[0][0] as CustomEvent;
      expect(call.detail).toEqual({ sourceId: "source-123" });
    });

    it("should not dispatch event when no source selected", async () => {
      mockDispatchEvent.mockClear();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources: [] },
        ])
      );

      render(<RightPane />);

      // No jump button should be visible
      expect(screen.queryByText("Jump to answer")).not.toBeInTheDocument();
      expect(mockDispatchEvent).not.toHaveBeenCalled();
    });
  });

  // ===========================================================================
  // ADDITIONAL EDGE CASES
  // ===========================================================================
  describe("Additional edge cases", () => {
    it("should handle sources with duplicate IDs", async () => {
      const sources = [
        createSource({ id: "dup-id", filename: "file1.txt" }),
        createSource({ id: "dup-id", filename: "file2.txt" }),
      ];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // Both sources should be in DOM
      expect(screen.queryAllByText("file1.txt").length).toBeGreaterThan(0);
    });

    it("should handle rapid source selection changes", async () => {
      const sources = Array.from({ length: 5 }, (_, i) => 
        createSource({ id: `src-${i}`, filename: `Document ${i}` })
      );
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // Get all source buttons and click them rapidly
      const buttons = screen.getAllByRole("button");
      const sourceButtons = buttons.filter(btn => btn.textContent?.includes("Document"));
      
      for (const button of sourceButtons) {
        fireEvent.click(button);
      }

      expect(screen.getByText("Evidence")).toBeInTheDocument();
    });

    it("should handle tab switching rapidly", async () => {
      const sources = [createSource()];
      const content = "```python\nprint('hello')\n```";
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content, sources },
        ])
      );

      render(<RightPane />);

      const sourcesTab = screen.getByRole("tab", { name: /Sources/i });
      const extractedTab = screen.getByRole("tab", { name: /Extracted/i });

      // Rapid tab switching
      for (let i = 0; i < 10; i++) {
        fireEvent.click(i % 2 === 0 ? sourcesTab : extractedTab);
      }

      expect(screen.getByText("Evidence")).toBeInTheDocument();
    });

    it("should handle source with circular reference", async () => {
      const circularSource: any = { 
        id: "src-1", 
        filename: "test.txt", 
        snippet: "content" 
      };
      circularSource.self = circularSource;

      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources: [circularSource] },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle emoji in source filename", async () => {
      const sources = [createSource({ filename: "📄 Report 🚀.txt" })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("📄 Report 🚀.txt")).toBeInTheDocument();
      });
    });

    it("should handle very long query (1000+ chars)", async () => {
      const longQuery = "search term ".repeat(100);
      const sources = [createSource({ snippet: "content" })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: longQuery },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });
    });

    it("should handle message with empty content and sources", async () => {
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "", sources: [] },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("No sources yet")).toBeInTheDocument();
      });
    });

    it("should handle markdown table without separator row", async () => {
      const content = `
| Header 1 | Header 2 |
| Cell 1   | Cell 2   |
| Cell 3   | Cell 4   |
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });
    });

    it("should handle empty markdown table", async () => {
      const content = `
|
|
      `.trim();
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });
  });

  // ===========================================================================
  // BOUNDARY VIOLATIONS
  // ===========================================================================
  describe("Boundary violations", () => {
    it("should handle Number.MAX_SAFE_INTEGER score", async () => {
      const sources = [createSource({ score: Number.MAX_SAFE_INTEGER })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle -0 score", async () => {
      const sources = [createSource({ score: -0 })];
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      expect(() => {
        render(<RightPane />);
      }).not.toThrow();
    });

    it("should handle 1000 sources", async () => {
      const sources = Array.from({ length: 1000 }, (_, i) => 
        createSource({ id: `src-${i}`, filename: `file${i}.txt` })
      );
      
      (useChatStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
        createMockMessages([
          { id: "msg-1", role: "user", content: "test" },
          { id: "msg-2", role: "assistant", content: "Results", sources },
        ])
      );

      render(<RightPane />);

      await waitFor(() => {
        expect(screen.getByText("Evidence")).toBeInTheDocument();
      });

      // Should show count in tab
      expect(screen.getByText("(1000)")).toBeInTheDocument();
    });
  });
});
