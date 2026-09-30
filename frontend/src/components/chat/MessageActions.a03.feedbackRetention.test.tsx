// frontend/src/components/chat/MessageActions.a03.feedbackRetention.test.tsx
// Issue #685 — persisted feedback mirrors must stay bounded. Every vote
// writes a chat_feedback_<messageId> key; on a long-lived client the retained
// key set must stay bounded instead of growing without limit. At the pre-fix
// tree a new vote for a never-voted message simply appends another key, so a
// client at the bound crosses it (1000 seeded keys -> 1001 after one vote).

import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { updateMessageFeedback } from "@/lib/api";
import { AssistantMessageActions } from "./MessageActions";

vi.mock("@/lib/api", () => ({
  updateMessageFeedback: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
  },
}));

const FEEDBACK_KEY_PREFIX = "chat_feedback_";
const FEEDBACK_KEYS_BOUND = 1000;

// setup.ts replaces localStorage with a silent no-op; this check needs real
// storage semantics, so install a functional in-memory localStorage backed by
// a Map (fresh per test). The backing map is exposed so the assertion counts
// the exact persisted key set.
const storageBacking = new Map<string, string>();

function installFunctionalLocalStorage(): void {
  storageBacking.clear();
  Object.defineProperty(window, "localStorage", {
    value: {
      getItem: (key: string) => storageBacking.get(key) ?? null,
      setItem: (key: string, value: string) => {
        storageBacking.set(key, String(value));
      },
      removeItem: (key: string) => {
        storageBacking.delete(key);
      },
      clear: () => {
        storageBacking.clear();
      },
      get length() {
        return storageBacking.size;
      },
      key: (index: number) => Array.from(storageBacking.keys())[index] ?? null,
    },
    writable: true,
    configurable: true,
  });
}

describe("AssistantMessageActions feedback retention (issue #685)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installFunctionalLocalStorage();
    vi.mocked(updateMessageFeedback).mockResolvedValue(
      {} as Awaited<ReturnType<typeof updateMessageFeedback>>
    );
  });

  it("feedback keys stay bounded after a new vote", async () => {
    // A long-lived client has already accumulated the bound of retained
    // feedback keys.
    for (let i = 0; i < FEEDBACK_KEYS_BOUND; i++) {
      storageBacking.set(`${FEEDBACK_KEY_PREFIX}${i}`, "up");
    }

    // A vote on a NEW numeric message id (never voted before).
    render(
      <AssistantMessageActions content="Answer" sessionId="7" messageId="5000" />
    );

    await act(async () => {
      fireEvent.click(screen.getByLabelText("Good response"));
      await Promise.resolve();
    });

    const feedbackKeys = Array.from(storageBacking.keys()).filter((key) =>
      key.startsWith(FEEDBACK_KEY_PREFIX)
    );
    expect(feedbackKeys.length).toBeLessThanOrEqual(FEEDBACK_KEYS_BOUND);
  });
});
