// frontend/src/components/chat/AssistantMessage.c09-support-cards.test.tsx
// PR #966 feedback round: AssistantMessage-level wiring pins for all three
// citation-card support chips. The frozen c09-support test counts /support/i
// matches for the WIKI chip only; these tests pin the assembled-message wiring
// for Memory and KMS cards too (deleting either card's citationConfidence prop
// fails here), the exact visible "Support NN%" text, the absent-score case,
// the disclaimer's title-attribute channel, and the never-"verified" rule.
// RED at base, GREEN after the fix.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AssistantMessage } from "./AssistantMessage";
import { useChatShellStore } from "@/stores/useChatShellStore";
import type { Message } from "@/stores/useChatStore";
import type { KMSReference, UsedMemory, WikiReference } from "@/lib/api";

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(),
}));

const MEMORY: UsedMemory = {
  id: "m1",
  memory_label: "M1",
  content: "Zebras roam the savanna in family groups.",
  category: null,
  tags: null,
  source: null,
};

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

const TOOLTIP_FRAGMENT = "lexical measure";

const createMessage = (overrides: Partial<Message> = {}): Message => ({
  id: "msg-c09-cards-1",
  role: "assistant",
  content: "Cites [M1], [W1] and [K1].",
  memoriesUsed: [MEMORY],
  wikiRefs: [WIKI],
  kmsRefs: [KMS],
  citationConfidence: { M1: 0.5, W1: 0.82, K1: 0 },
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
  (useChatShellStore as unknown as vi.Mock).mockReturnValue({
    openRightPane: vi.fn(),
    setSelectedEvidenceSource: vi.fn(),
    setSelectedEvidenceMessageId: vi.fn(),
    setEvidenceReturnFocusId: vi.fn(),
    setActiveRightTab: vi.fn(),
  });
});

const renderMessage = (message: Message) =>
  render(
    <MemoryRouter>
      <AssistantMessage message={message} />
    </MemoryRouter>
  );

describe("AssistantMessage support-chip wiring for all card families (#966 feedback)", () => {
  it("renders the exact Support text for the memory, wiki and KMS cards", () => {
    renderMessage(createMessage());

    expect(screen.getByText("Support 50%")).toBeInTheDocument();
    expect(screen.getByText("Support 82%")).toBeInTheDocument();
    expect(screen.getByText("Support 0%")).toBeInTheDocument();
  });

  it("carries the lexical-measure disclaimer on the title attribute and never says verified", () => {
    renderMessage(createMessage());

    const chips = screen.getAllByText(/support/i);
    expect(chips.length).toBe(3);
    for (const chip of chips) {
      expect(chip.getAttribute("title")).toContain(TOOLTIP_FRAGMENT);
      expect(chip.getAttribute("title")?.toLowerCase()).not.toContain("verified");
    }
    expect(screen.queryByText(/verified/i)).not.toBeInTheDocument();
  });

  it("renders no support indicator when the message carries no citationConfidence", () => {
    renderMessage(
      createMessage({ citationConfidence: undefined })
    );

    expect(screen.getAllByRole("article").length).toBeGreaterThanOrEqual(3);
    expect(screen.queryAllByText(/support/i)).toHaveLength(0);
  });
});
