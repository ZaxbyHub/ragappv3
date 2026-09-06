// frontend/src/components/chat/AssistantMessage.test.tsx
// Unit tests for AssistantMessage component

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AssistantMessage, parseCitations } from "./AssistantMessage";
import { useChatShellStore } from "@/stores/useChatShellStore";
import type { Message } from "@/stores/useChatStore";
import type { Source } from "@/lib/api";

// Mock the store
vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(),
}));

// Mock navigator.clipboard
Object.assign(navigator, {
  clipboard: {
    writeText: vi.fn().mockResolvedValue(undefined),
  },
});

const mockOpenRightPane = vi.fn();
const mockSetSelectedEvidenceSource = vi.fn();
const mockSetSelectedEvidenceMessageId = vi.fn();
const mockSetEvidenceReturnFocusId = vi.fn();
const mockSetActiveRightTab = vi.fn();

// Setup default mock return values
beforeEach(() => {
  vi.clearAllMocks();
  (useChatShellStore as unknown as vi.Mock).mockReturnValue({
    openRightPane: mockOpenRightPane,
    setSelectedEvidenceSource: mockSetSelectedEvidenceSource,
    setSelectedEvidenceMessageId: mockSetSelectedEvidenceMessageId,
    setEvidenceReturnFocusId: mockSetEvidenceReturnFocusId,
    setActiveRightTab: mockSetActiveRightTab,
  });
});

// =============================================================================
// TEST DATA
// =============================================================================

const createMessage = (overrides: Partial<Message> = {}): Message => ({
  id: "msg-1",
  role: "assistant",
  content: "This is a test response.",
  sources: [],
  ...overrides,
});

const createSource = (overrides: Partial<Source> = {}): Source => ({
  id: "src-1",
  filename: "document.pdf",
  snippet: "Sample snippet",
  score: 0.2,
  score_type: "distance",
  ...overrides,
});

// =============================================================================
// parseCitations TESTS
// =============================================================================

describe("parseCitations", () => {
  it("should return empty segments for empty content", () => {
    const result = parseCitations("", []);
    expect(result.segments).toHaveLength(0);
    expect(result.citedSources).toHaveLength(0);
  });

  it("should parse single citation returns citation segment", () => {
    const sources = [createSource({ filename: "document.pdf" })];
    const result = parseCitations("See [Source: document.pdf] for details.", sources);

    // Segments: text "See " + citation + text " for details."
    expect(result.segments).toHaveLength(3);
    expect(result.segments[0]).toEqual({ type: "text", content: "See " });
    expect(result.segments[1]).toEqual({ type: "citation", sourceName: "document.pdf" });
    expect(result.segments[2]).toEqual({ type: "text", content: " for details." });
    expect(result.citedSources).toHaveLength(1);
    expect(result.citedSources[0].filename).toBe("document.pdf");
  });

  it("should parse multiple citations returns correct segments", () => {
    const sources = [
      createSource({ id: "src-1", filename: "doc1.pdf" }),
      createSource({ id: "src-2", filename: "doc2.pdf" }),
    ];
    const result = parseCitations(
      "See [Source: doc1.pdf] and [Source: doc2.pdf] for more info.",
      sources
    );

    // Segments: text "See " + citation + text " and " + citation + text " for more info."
    expect(result.segments).toHaveLength(5);
    expect(result.citedSources).toHaveLength(2);
  });

  it("should deduplicate cited sources", () => {
    const sources = [createSource({ id: "src-1", filename: "doc.pdf" })];
    const result = parseCitations(
      "See [Source: doc.pdf] and again [Source: doc.pdf].",
      sources
    );

    // Segments: text + citation + text + citation + text
    expect(result.segments).toHaveLength(5);
    expect(result.citedSources).toHaveLength(1);
  });

  it("should handle citation with source not in sources list", () => {
    const result = parseCitations("See [Source: missing.pdf] for info.", []);

    // Segments: text + citation + text
    expect(result.segments).toHaveLength(3);
    expect(result.segments[1]).toEqual({ type: "citation", sourceName: "missing.pdf" });
    expect(result.citedSources).toHaveLength(0);
  });

  it("should handle content without citations", () => {
    const result = parseCitations("Just plain text content.", []);

    expect(result.segments).toHaveLength(1);
    expect(result.segments[0]).toEqual({ type: "text", content: "Just plain text content." });
    expect(result.citedSources).toHaveLength(0);
  });

  it("should handle citation at start of content", () => {
    const sources = [createSource({ filename: "start.pdf" })];
    const result = parseCitations("[Source: start.pdf] begins here.", sources);

    expect(result.segments[0]).toEqual({ type: "citation", sourceName: "start.pdf" });
    expect(result.segments[1]).toEqual({ type: "text", content: " begins here." });
  });

  it("should handle citation at end of content", () => {
    const sources = [createSource({ filename: "end.pdf" })];
    const result = parseCitations("Ends with [Source: end.pdf]", sources);

    expect(result.segments[result.segments.length - 1]).toEqual({
      type: "citation",
      sourceName: "end.pdf",
    });
  });

  it("should handle whitespace in source name", () => {
    const sources = [createSource({ filename: "my document.pdf" })];
    const result = parseCitations("See [Source: my document.pdf] for details.", sources);

    expect(result.segments[1]).toEqual({ type: "citation", sourceName: "my document.pdf" });
  });

  // New stable label [S#] citation tests
  it("should parse [S1] stable source labels", () => {
    const sources = [
      createSource({ id: "src-1", filename: "doc1.pdf", source_label: "S1" }),
    ];
    const result = parseCitations("According to [S1], the answer is yes.", sources);

    expect(result.segments).toHaveLength(3);
    expect(result.segments[0]).toEqual({ type: "text", content: "According to " });
    expect(result.segments[1]).toEqual({ type: "citation", sourceName: "S1" });
    expect(result.segments[2]).toEqual({ type: "text", content: ", the answer is yes." });
    expect(result.citedSources).toHaveLength(1);
    expect(result.citedSources[0].id).toBe("src-1");
  });

  it("should parse multiple [S#] labels", () => {
    const sources = [
      createSource({ id: "src-1", filename: "doc1.pdf", source_label: "S1" }),
      createSource({ id: "src-2", filename: "doc2.pdf", source_label: "S2" }),
    ];
    const result = parseCitations("See [S1] and [S2] for details.", sources);

    expect(result.citedSources).toHaveLength(2);
    expect(result.citedSources[0].id).toBe("src-1");
    expect(result.citedSources[1].id).toBe("src-2");
  });

  it("should resolve [S#] by index when source_label is missing", () => {
    const sources = [
      createSource({ id: "src-1", filename: "doc1.pdf" }),
      createSource({ id: "src-2", filename: "doc2.pdf" }),
    ];
    const result = parseCitations("Refer to [S2] for context.", sources);

    expect(result.citedSources).toHaveLength(1);
    expect(result.citedSources[0].id).toBe("src-2");
  });

  it("should handle duplicate filenames with distinct [S#] labels", () => {
    const sources = [
      createSource({ id: "src-1", filename: "report.pdf", source_label: "S1" }),
      createSource({ id: "src-2", filename: "report.pdf", source_label: "S2" }),
    ];
    const result = parseCitations("Compare [S1] and [S2].", sources);

    expect(result.citedSources).toHaveLength(2);
    expect(result.citedSources[0].id).toBe("src-1");
    expect(result.citedSources[1].id).toBe("src-2");
  });

  it("should handle mixed legacy and new citation formats", () => {
    const sources = [
      createSource({ id: "src-1", filename: "doc1.pdf", source_label: "S1" }),
      createSource({ id: "src-2", filename: "legacy.pdf" }),
    ];
    const result = parseCitations("See [S1] and [Source: legacy.pdf].", sources);

    expect(result.citedSources).toHaveLength(2);
  });
});

// =============================================================================
// AssistantMessage COMPONENT TESTS
// =============================================================================

describe("AssistantMessage", () => {
  it("should render assistant avatar and name", () => {
    const message = createMessage();
    render(<AssistantMessage message={message} />);

    expect(screen.getByText("Assistant")).toBeInTheDocument();
    expect(screen.getByLabelText("Assistant message")).toBeInTheDocument();
  });

  it("should render message content", () => {
    const message = createMessage({ content: "Hello, this is the assistant speaking." });
    render(<AssistantMessage message={message} />);

    expect(screen.getByText("Hello, this is the assistant speaking.")).toBeInTheDocument();
  });

  it("should render markdown content", () => {
    const message = createMessage({ content: "This is **bold** and *italic* text." });
    render(<AssistantMessage message={message} />);

    // Check that the content is rendered (markdown processed)
    expect(screen.getByText(/bold/)).toBeInTheDocument();
    expect(screen.getByText(/italic/)).toBeInTheDocument();
  });

  it("should not render bounce dots inline when isStreaming is true and content is empty (WaitingIndicator is at TranscriptPane level)", () => {
    const message = createMessage({ content: "" });
    render(<AssistantMessage message={message} isStreaming={true} />);

    // The component renders the message wrapper but inline bounce dots were removed
    // (WaitingIndicator is at TranscriptPane level). ActionBar must be absent while streaming.
    expect(screen.getByLabelText("Assistant message")).toBeInTheDocument();
    expect(screen.queryByLabelText("Copy message")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Retry")).not.toBeInTheDocument();
  });

  it("should not show Thinking text when isStreaming is false", () => {
    const message = createMessage({ content: "" });
    render(<AssistantMessage message={message} isStreaming={false} />);

    // No inline streaming indicator expected in either state
    expect(screen.queryByText("Thinking")).not.toBeInTheDocument();
  });

  it("should render error message", () => {
    const message = createMessage({ error: "Something went wrong" });
    render(<AssistantMessage message={message} />);

    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  });

  it("should render stopped indicator", () => {
    const message = createMessage({ stopped: true });
    render(<AssistantMessage message={message} />);

    expect(screen.getByText("Stopped")).toBeInTheDocument();
  });

  it("should call onCopy when copy button is clicked", async () => {
    const onCopy = vi.fn();
    const message = createMessage({ content: "Copy me" });
    render(<AssistantMessage message={message} onCopy={onCopy} />);

    const copyButton = screen.getByLabelText("Copy message");
    fireEvent.click(copyButton);

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("Copy me");
      expect(onCopy).toHaveBeenCalled();
    });
  });

  it("should call onRetry when retry button is clicked", () => {
    const onRetry = vi.fn();
    const message = createMessage();
    render(<AssistantMessage message={message} onRetry={onRetry} />);

    const retryButton = screen.getByLabelText("Retry");
    fireEvent.click(retryButton);

    expect(onRetry).toHaveBeenCalled();
  });

  it("should toggle debug mode when debug button is clicked", () => {
    const onDebugToggle = vi.fn();
    const message = createMessage({ id: "debug-msg", content: "Debug test" });
    render(<AssistantMessage message={message} onDebugToggle={onDebugToggle} />);

    const debugButton = screen.getByLabelText("Toggle debug info");
    fireEvent.click(debugButton);

    expect(onDebugToggle).toHaveBeenCalledWith(true);
    expect(screen.getByText("Debug Info:")).toBeInTheDocument();
    expect(screen.getByText(/Message ID: debug-msg/)).toBeInTheDocument();
  });

  it("should not show action buttons when streaming", () => {
    const message = createMessage();
    render(<AssistantMessage message={message} isStreaming={true} />);

    expect(screen.queryByLabelText("Copy message")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Retry")).not.toBeInTheDocument();
  });
});

// =============================================================================
// CITATION & SOURCES TESTS
// =============================================================================

describe("AssistantMessage - Citations and Sources", () => {
  it("should render citation chips for inline citations", () => {
    const sources = [
      createSource({ id: "src-1", filename: "report.pdf" }),
    ];
    const message = createMessage({
      content: "According to [Source: report.pdf], the data shows...",
      sources,
    });
    render(<AssistantMessage message={message} />);

    // Inline + strip variants share aria-label so assistive tech announces the
    // filename either way, but only one carries the full filename as text.
    const citationChips = screen.getAllByLabelText("Source S1: report.pdf");
    expect(citationChips.length).toBeGreaterThanOrEqual(2);
  });

  it("should render inline citation as a compact number-only pill while the evidence strip keeps the full filename", () => {
    const sources = [
      createSource({ id: "src-1", filename: "report.pdf", source_label: "S1" }),
    ];
    const message = createMessage({
      content: "According to [S1], the data shows...",
      sources,
    });
    render(<AssistantMessage message={message} />);

    const chips = screen.getAllByLabelText("Source S1: report.pdf");
    // Both variants render: one inline pill (text="1") and one strip chip
    // (text contains the filename). This keeps per-sentence attribution
    // without duplicating the heavy filename chip inside prose.
    expect(chips.length).toBe(2);

    const inlinePill = chips.find((el) => el.textContent?.trim() === "S1");
    const stripChip = chips.find((el) => el.textContent?.includes("report.pdf"));
    expect(inlinePill).toBeDefined();
    expect(stripChip).toBeDefined();
  });

  it("should open right pane when citation chip is clicked", () => {
    const sources = [createSource({ id: "src-1", filename: "doc.pdf" })];
    const message = createMessage({
      content: "See [Source: doc.pdf] for more.",
      sources,
    });
    render(<AssistantMessage message={message} />);

    // Click the first citation chip (inline)
    const citationChips = screen.getAllByLabelText("Source S1: doc.pdf");
    fireEvent.click(citationChips[0]);

    expect(mockSetSelectedEvidenceSource).toHaveBeenCalledWith(sources[0]);
    expect(mockSetActiveRightTab).toHaveBeenCalledWith("evidence");
    expect(mockOpenRightPane).toHaveBeenCalled();
  });

  it("records the clicked message as the evidence selection anchor and focus target", () => {
    const sources = [createSource({ id: "src-1", filename: "doc.pdf" })];
    const message = createMessage({
      id: "anchor-msg",
      content: "See [Source: doc.pdf] for more.",
      sources,
    });
    render(<AssistantMessage message={message} />);

    const citationChips = screen.getAllByLabelText("Source S1: doc.pdf");
    fireEvent.click(citationChips[0]);

    expect(mockSetSelectedEvidenceMessageId).toHaveBeenCalledWith("anchor-msg");
    expect(mockSetEvidenceReturnFocusId).toHaveBeenCalledWith("anchor-msg");
  });

  it("should render evidence strip with sources", () => {
    const sources = [
      createSource({ id: "src-1", filename: "file1.pdf" }),
      createSource({ id: "src-2", filename: "file2.pdf" }),
    ];
    const message = createMessage({ content: "[S1] [S2] Test message", sources });
    render(<AssistantMessage message={message} />);

    expect(screen.getByText("Sources:")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Source S1: file1.pdf").length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Source S2: file2.pdf").length).toBeGreaterThan(0);
  });

  it("should show '+N more' when there are more than 3 sources", () => {
    const sources = [
      createSource({ id: "src-1", filename: "file1.pdf" }),
      createSource({ id: "src-2", filename: "file2.pdf" }),
      createSource({ id: "src-3", filename: "file3.pdf" }),
      createSource({ id: "src-4", filename: "file4.pdf" }),
    ];
    const message = createMessage({ content: "[S1] [S2] [S3] [S4] Test message", sources });
    render(<AssistantMessage message={message} />);

    expect(screen.getByText("+1 more")).toBeInTheDocument();
  });

  it("should show 'View all' only when there are more than 3 sources", () => {
    const sources = [
      createSource({ id: "src-1", filename: "file1.pdf" }),
      createSource({ id: "src-2", filename: "file2.pdf" }),
      createSource({ id: "src-3", filename: "file3.pdf" }),
      createSource({ id: "src-4", filename: "file4.pdf" }),
    ];
    const message = createMessage({ content: "[S1] [S2] [S3] [S4] Test message", sources });
    render(<AssistantMessage message={message} />);

    // View all only shows when sources.length > 3
    expect(screen.getByLabelText("View all 4 sources")).toBeInTheDocument();
  });

  it("should open right pane when '+N more' is clicked", () => {
    const sources = [
      createSource({ id: "src-1", filename: "file1.pdf" }),
      createSource({ id: "src-2", filename: "file2.pdf" }),
      createSource({ id: "src-3", filename: "file3.pdf" }),
      createSource({ id: "src-4", filename: "file4.pdf" }),
    ];
    const message = createMessage({ content: "[S1] [S2] [S3] [S4] Test message", sources });
    render(<AssistantMessage message={message} />);

    const moreButton = screen.getByLabelText("View all 4 sources");
    fireEvent.click(moreButton);

    expect(mockSetActiveRightTab).toHaveBeenCalledWith("evidence");
    expect(mockOpenRightPane).toHaveBeenCalled();
  });

  it("should show relevance badges for sources with scores", () => {
    const sources = [
      createSource({ id: "src-1", filename: "relevant.pdf", score: 0.2, score_type: "distance" }),
      createSource({ id: "src-2", filename: "highly-relevant.pdf", score: 0.1, score_type: "distance" }),
    ];
    const message = createMessage({ content: "[S1] [S2] Test message", sources });
    render(<AssistantMessage message={message} />);

    // Should show "Relevant" and "Highly Relevant" badges
    expect(screen.getByText("Relevant")).toBeInTheDocument();
    expect(screen.getByText("Highly Relevant")).toBeInTheDocument();
  });
});

// =============================================================================
// SOURCE CLICK HANDLER TESTS
// =============================================================================

describe("AssistantMessage - onSourceClick", () => {
  it("should call onSourceClick when source is clicked", () => {
    const onSourceClick = vi.fn();
    const sources = [createSource({ id: "src-1", filename: "doc.pdf" })];
    const message = createMessage({
      content: "See [Source: doc.pdf] for more.",
      sources,
    });
    render(
      <AssistantMessage message={message} onSourceClick={onSourceClick} />
    );

    const citationChips = screen.getAllByLabelText("Source S1: doc.pdf");
    fireEvent.click(citationChips[0]);

    expect(onSourceClick).toHaveBeenCalledWith(sources[0]);
  });

  it("should handle source click from evidence strip", () => {
    const onSourceClick = vi.fn();
    const sources = [createSource({ id: "src-1", filename: "evidence.pdf" })];
    const message = createMessage({ content: "[S1] Test message", sources });
    render(
      <AssistantMessage message={message} onSourceClick={onSourceClick} />
    );

    const sourceChips = screen.getAllByLabelText("Source S1: evidence.pdf");
    fireEvent.click(sourceChips[0]);

    expect(onSourceClick).toHaveBeenCalledWith(sources[0]);
    expect(mockSetSelectedEvidenceSource).toHaveBeenCalledWith(sources[0]);
  });
});

// =============================================================================
// HOVER ACTIONS & INTERACTION TESTS
// =============================================================================

describe("AssistantMessage - Hover Actions", () => {
  it("should show hover actions on mouse enter", () => {
    const message = createMessage({ content: "Test message" });
    render(<AssistantMessage message={message} />);

    const messageContainer = screen.getByLabelText("Assistant message");

    // Initially action bar should be hidden (opacity-60 from group-hover pattern)
    const actionBar = messageContainer.querySelector('[class*="opacity-60"]');
    expect(actionBar).toBeInTheDocument();

    // After mouse enter, action buttons should be in the DOM
    const copyButton = screen.getByLabelText("Copy message");
    expect(copyButton).toBeInTheDocument();
    // The button is rendered but hidden via opacity, it's still in the DOM
  });

  it("should call onViewAllSources when View all is clicked", () => {
    const onViewAllSources = vi.fn();
    const sources = [
      createSource({ id: "src-1", filename: "file1.pdf" }),
      createSource({ id: "src-2", filename: "file2.pdf" }),
      createSource({ id: "src-3", filename: "file3.pdf" }),
      createSource({ id: "src-4", filename: "file4.pdf" }),
    ];
    const message = createMessage({ content: "[S1] [S2] [S3] [S4] Test message", sources });
    render(
      <AssistantMessage message={message} onViewAllSources={onViewAllSources} />
    );

    const viewAllButton = screen.getByLabelText("View all 4 sources");
    fireEvent.click(viewAllButton);

    expect(mockSetActiveRightTab).toHaveBeenCalledWith("evidence");
    expect(mockOpenRightPane).toHaveBeenCalled();
    expect(onViewAllSources).toHaveBeenCalled();
  });
});
