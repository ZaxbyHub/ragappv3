// frontend/src/components/chat/RightPane.c09-support.test.tsx
// Issue #965: the RightPane evidence tabs render the same WikiCards/KMSCards
// components the assistant message does, so cited wiki/KMS cards in the side
// pane must surface the same "Support NN%" chip from the done payload's
// per-label citationConfidence scores (AC1/AC2 RED at base: the pane passed no
// citationConfidence and the selector did not exist, so queryAllByText finds
// 0). AC3 is the preserving case: absent scores keep the no-chip rendering.
// Runs against the REAL useChatStore selectors and the REAL card components —
// only the shell store, the API barrel and the Tabs chrome are mocked.
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { RightPane } from "./RightPane";
import { useChatStore, type Message } from "@/stores/useChatStore";
import type { KMSReference, WikiReference } from "@/lib/api";

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(() => ({
    selectedEvidenceSource: null,
    selectedEvidenceMessageId: null,
    setSelectedEvidenceSource: vi.fn(),
    setSelectedEvidenceMessageId: vi.fn(),
    setEvidenceReturnFocusId: vi.fn(),
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
  getDocumentRawBlob: vi.fn(),
  getArtifactRawBlob: vi.fn(),
}));

// Mock the Tabs chrome so every TabsContent (including wiki/kms) mounts
// without interaction, matching the established RightPane.test.tsx pattern.
vi.mock("@/components/ui/tabs", () => ({
  Tabs: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  TabsList: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  TabsTrigger: ({ children }: { children?: ReactNode }) => (
    <button type="button">{children}</button>
  ),
  TabsContent: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
}));

// jsdom does not implement scrollIntoView; stub defensively before any render.
Element.prototype.scrollIntoView = vi.fn();

const WIKI: WikiReference = {
  wiki_label: "W1",
  page_id: 1,
  claim_id: null,
  title: "Savanna fauna",
  slug: null,
  page_type: null,
  claim_text: "Zebras roam the savanna in herds.",
  excerpt: null,
  confidence: 0.9,
  status: "active",
  page_status: "verified",
  claim_status: "active",
  score: 1,
  score_type: null,
  source_count: 1,
  provenance_summary: "",
};

const KMS: KMSReference = {
  kms_label: "K1",
  entry_id: 7,
  slug: "pythagorean-theorem",
  title: "Pythagorean theorem",
  summary: null,
  excerpt: "The pythagorean theorem relates triangle sides.",
  tags: [],
  status: "active",
  source_type: null,
  file_id: null,
  score: 1,
  score_type: null,
};

const USER: Message = { id: "u1", role: "user", content: "What do the sources say?" };

beforeEach(() => {
  useChatStore.setState({
    messageIds: [],
    messagesById: {},
    streamingMessageId: null,
    input: "",
    isStreaming: false,
    abortFn: null,
    inputError: null,
    expandedSources: new Set(),
    activeChatId: null,
  });
});

/** Seed one completed exchange: a user turn + the given assistant message. */
const seedStore = (assistant: Partial<Message> & Pick<Message, "id">) => {
  useChatStore.setState({
    messageIds: [USER.id, assistant.id],
    messagesById: {
      [USER.id]: USER,
      [assistant.id]: {
        role: "assistant",
        content: "Cites the evidence.",
        ...assistant,
      },
    },
    streamingMessageId: null,
  });
};

const renderPane = () =>
  render(
    <MemoryRouter>
      <RightPane />
    </MemoryRouter>
  );

describe("RightPane support chips on side-pane evidence cards (#965)", () => {
  it("renders a support score for a cited wiki ref in the pane", () => {
    seedStore({ id: "a1", wikiRefs: [WIKI], citationConfidence: { W1: 0.5 } });

    renderPane();

    const chips = screen.getAllByText(/support/i);
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveTextContent("Support 50%");
    // #714 chip contract reused verbatim: the lexical-measure disclaimer lives
    // only in the title attribute, never a text node, and nothing here claims
    // "verified" (the card's own status label is "active").
    expect(chips[0].getAttribute("title")).toContain("lexical measure");
    expect(chips[0].getAttribute("title")?.toLowerCase()).not.toContain("verified");
    expect(screen.queryByText(/verified/i)).not.toBeInTheDocument();
  });

  it("renders a support score for a cited KMS ref in the pane", () => {
    seedStore({ id: "a1", kmsRefs: [KMS], citationConfidence: { K1: 0 } });

    renderPane();

    const chips = screen.getAllByText(/support/i);
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveTextContent("Support 0%");
    expect(chips[0].getAttribute("title")).toContain("lexical measure");
    expect(chips[0].getAttribute("title")?.toLowerCase()).not.toContain("verified");
  });

  it("renders no support indicator without scores", () => {
    seedStore({ id: "a1", wikiRefs: [WIKI], kmsRefs: [KMS] });
    renderPane();

    // Both cited cards render (role=article); neither carries a chip.
    expect(screen.getAllByRole("article").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryAllByText(/support/i)).toHaveLength(0);

    // An explicitly empty confidence record changes nothing either.
    act(() => {
      seedStore({
        id: "a1",
        wikiRefs: [WIKI],
        kmsRefs: [KMS],
        citationConfidence: {},
      });
    });
    expect(screen.queryAllByText(/support/i)).toHaveLength(0);
  });
});
