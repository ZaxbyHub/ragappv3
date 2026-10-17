// frontend/src/components/chat/AssistantMessage.c09-support.test.tsx
// Issue #714 AC4: AssistantMessage surfaces a per-citation support score for
// a cited wiki [W#] ref. RED at base, GREEN after the fix — at base the wiki
// card renders but carries no "support" score text anywhere.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render as rtlRender, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AssistantMessage } from "./AssistantMessage";
import { useChatShellStore } from "@/stores/useChatShellStore";
import type { Message } from "@/stores/useChatStore";
import type { WikiReference } from "@/lib/api";

vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn(),
}));

const WIKI_REF: WikiReference = {
  wiki_label: "W1",
  page_id: 3,
  claim_id: 10,
  title: "Savanna zebras",
  slug: "savanna-zebras",
  page_type: "note",
  claim_text: "Zebras roam the savanna in mixed herds.",
  excerpt: null,
  confidence: 0.9,
  status: null,
  page_status: "completed",
  claim_status: null,
  score: 0.85,
  score_type: "page_fts",
  source_count: 2,
  provenance_summary: "2 documents",
};

const createMessage = (overrides: Partial<Message> = {}): Message => ({
  id: "msg-c09-1",
  role: "assistant",
  content: "Zebras roam the savanna [W1].",
  // No sources on purpose: with no message.sources there are no source
  // cards, so nothing but the wiki card can match /support/i.
  ...overrides,
});

// WikiCard calls useNavigate(); components calling router hooks need a
// Router context (see frontend-testing-gotchas).
const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

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

describe("AssistantMessage support score for wiki citations (issue #714)", () => {
  it("renders a support score for a cited wiki ref", () => {
    render(
      <AssistantMessage
        message={createMessage({
          wikiRefs: [WIKI_REF],
          citationConfidence: { W1: 0 },
        })}
      />
    );

    // Guard: the wiki card for the cited ref actually rendered.
    expect(
      screen.getByRole("article", { name: /Wiki W1: Savanna zebras/i })
    ).toBeInTheDocument();
    // Exactly one element says "support"; nothing claims "verified".
    expect(screen.queryAllByText(/support/i).length).toBe(1);
    expect(screen.queryByText(/verified/i)).toBeNull();
  });
});
