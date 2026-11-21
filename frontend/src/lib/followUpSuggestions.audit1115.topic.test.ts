// frontend/src/lib/followUpSuggestions.audit1115.topic.test.ts
// Issue #1153 AC6 (arm's-length check C6): follow-up suggestion topics must
// come from the ASSISTANT ANSWER, not from the user's question.
//
// POST-FIX SIGNATURE THIS SPEC IS AUTHORED AGAINST:
//   deriveFollowUps(assistantContent: string, sourceTitles: string[]): string[]
// The FIRST argument is the assistant's answer; the topic embedded in the
// chips is derived from that answer. The user's question is no longer an
// input to the derivation.
//
// Leg structure:
// - Two unit legs pin the fixed call shape: calling deriveFollowUps with the
//   assistant ANSWER as the first argument yields chips that embed a topic
//   phrase from the answer (these hold at base too -- the base function
//   derives its topic from whatever string is passed first -- so they are
//   the preserving half of the pin).
// - The RED-at-base leg exercises the PRODUCTION CALL SITE through the real
//   TranscriptPane: the pane derives chips for a completed exchange whose
//   user question and assistant answer carry DIFFERENT topics. At base the
//   pane passes the USER question (TranscriptPane.tsx:1006), so the chips
//   embed the user's topic and never the answer's; the fixed contract
//   embeds the ANSWER's topic. A same-shape string-for-string swap of the
//   unit call cannot discriminate base from fixed in isolation (the
//   function cannot tell whose prose the string is), so the call-site leg
//   carries the discrimination; weakening it is not an option.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import type { ReactNode } from "react";
import { deriveFollowUps } from "./followUpSuggestions";

const USER_QUESTION = "What is the vendor onboarding process for new suppliers?";
const ASSISTANT_ANSWER = "The coolant interval is 60,000 km for turbo engines.";

vi.mock("@/lib/api", () => ({
  getSettings: async () => ({ chat_message_max_chars: 20000 }),
  getChatSession: vi.fn(async () => ({ id: 1, vault_id: 1, messages: [] })),
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: vi.fn(),
  truncateChatSession: vi.fn(),
  forkChatSession: vi.fn(),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(() => ({ user: null })),
}));

vi.mock("@/hooks/useChatHistory", () => ({
  useChatHistory: () => ({
    refreshHistory: vi.fn(),
    chatHistory: [],
    isChatLoading: false,
    chatHistoryError: null,
  }),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
  useInRouterContext: () => false,
  useLocation: () => ({ pathname: "/chat" }),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

vi.mock("framer-motion", () => ({
  motion: {
    div: (props: { children?: ReactNode }) =>
      createElement("div", { "data-testid": "motion-div" }, props.children),
  },
  AnimatePresence: ({ children }: { children?: ReactNode }) =>
    createElement("div", null, children),
  useReducedMotion: () => false,
}));

vi.mock("@/components/chat/MessageBubble", () => ({
  MessageBubble: ({ message }: { message: { id: string; content: string } }) =>
    createElement(
      "div",
      { "data-testid": "message-bubble", "data-message-id": message.id },
      message.content,
    ),
}));

vi.mock("@/components/chat/AssistantMessage", () => ({
  AssistantMessage: ({ message }: { message: { id: string; content: string } }) =>
    createElement(
      "div",
      { "data-testid": "assistant-message", "data-message-id": message.id },
      message.content,
    ),
}));

import { TranscriptPane } from "@/components/chat/TranscriptPane";
import { useChatStore } from "@/stores/useChatStore";
import { mapSessionMessage } from "@/lib/chatMessageMapper";

describe("deriveFollowUps topic source (issue #1153 AC6)", () => {
  describe("fixed-signature unit contract: first argument is the assistant answer", () => {
    it("embeds a topic phrase FROM THE ANSWER in the shipped chips", () => {
      const chips = deriveFollowUps(ASSISTANT_ANSWER, ["Service Manual.pdf"]);
      expect(chips.length).toBeGreaterThan(0);
      expect(chips.some((chip) => /coolant interval/i.test(chip))).toBe(true);
    });

    it("produces answer-grounded chips from a declarative answer with no question input", () => {
      const chips = deriveFollowUps(
        "Coolant must be replaced every 60,000 km on turbo engines.",
        [],
      );
      expect(chips.some((chip) => /coolant/i.test(chip))).toBe(true);
    });
  });

  describe("production call site: the pane derives the topic from the assistant answer", () => {
    beforeEach(() => {
      vi.clearAllMocks();
      act(() => {
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
          pendingTurnPersist: null,
          messageEditVersions: {},
          activeEditVersion: {},
        });
        // A completed exchange whose two halves carry DIFFERENT topics.
        useChatStore.getState().loadChat("42", [
          mapSessionMessage({
            id: 1,
            role: "user",
            content: USER_QUESTION,
            sources: null,
            created_at: "2026-11-01T00:00:01Z",
            seq: 1,
          }),
          mapSessionMessage({
            id: 2,
            role: "assistant",
            content: ASSISTANT_ANSWER,
            sources: [
              { id: "s1", filename: "Service Manual.pdf" },
            ],
            created_at: "2026-11-01T00:00:02Z",
            seq: 2,
            status: "complete",
          }),
        ]);
      });
    });

    afterEach(() => {
      cleanup();
    });

    it("renders chips that embed the answer topic, not the user question topic", async () => {
      render(createElement(TranscriptPane));
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });

      // Base renders this section (its chips carry the USER topic), so the
      // section is present; the discrimination is in WHICH topic it embeds.
      const section = screen.getByLabelText("Suggested follow-ups");
      expect(section).toHaveTextContent(/coolant interval/i);
      expect(section).not.toHaveTextContent(/vendor onboarding/i);
    });
  });
});
