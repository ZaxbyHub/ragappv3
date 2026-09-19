// frontend/src/components/chat/ShareAction.test.tsx
// Issue #573 (AC5) — acceptance check C5: share-link action.
//
// DISCRIMINATING check: expected RED at base commit ae2e15a0 — no share
// action exists anywhere (only the Markdown Export download in ChatShell).
// GREEN once the implementer ships the component and wires it into ChatShell.
//
// DECISION (recorded in the trace): Share = copying an authenticated session
// link `${origin}/chat/${sessionId}` to the clipboard — DISTINCT from the
// existing Markdown export, which downloads a .md Blob via an anchor.
//
// Frozen component contract:
//   File:    frontend/src/components/chat/ShareAction.tsx
//   Export:  ShareAction
//   Props:   { sessionId: string }
//   Behavior:
//     - renders a button whose accessible name matches /share/i;
//     - activating it copies `${window.location.origin}/chat/${sessionId}`
//       to the clipboard via navigator.clipboard.writeText (an http(s) URL
//       containing "/chat/<sessionId>");
//     - it does NOT download a file: no URL.createObjectURL call and no
//       blob: payload (the clipboard payload is the URL string itself).
//   Wiring:  frontend/src/pages/ChatShell.tsx references ShareAction
//            (source-scan guardrail).

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { ShareAction } from "./ShareAction";

const CHAT_SHELL_PATH = resolve(__dirname, "../../pages/ChatShell.tsx");

describe("ShareAction component contract (issue #573 AC5 / C5)", () => {
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    // jsdom may or may not implement URL.createObjectURL; install a spy
    // either way so a regression to the blob-download path is detectable.
    if (!("createObjectURL" in URL)) {
      Object.defineProperty(URL, "createObjectURL", {
        value: vi.fn(() => "blob:stub"),
        writable: true,
        configurable: true,
      });
    }
    vi.spyOn(URL, "createObjectURL");
  });

  it("copies an authenticated session URL containing /chat/<sessionId> to the clipboard on activation", () => {
    render(<ShareAction sessionId="42" />);

    fireEvent.click(screen.getByRole("button", { name: /share/i }));

    expect(writeText).toHaveBeenCalledTimes(1);
    const payload = writeText.mock.calls[0][0] as string;
    expect(payload).toMatch(/^https?:\/\//);
    expect(payload).toContain("/chat/42");
  });

  it("is a clipboard copy, not a file download (no object URL, no blob: payload)", () => {
    render(<ShareAction sessionId="42" />);

    fireEvent.click(screen.getByRole("button", { name: /share/i }));

    expect(
      URL.createObjectURL,
      "ShareAction must not download a file — the share decision is a clipboard link, unlike the Markdown export"
    ).not.toHaveBeenCalled();
    const payload = writeText.mock.calls[0][0] as string;
    expect(payload).not.toMatch(/^blob:/);
    expect(
      payload.startsWith("http"),
      "the clipboard payload must be the share URL string itself"
    ).toBe(true);
  });
});

describe("ShareAction wiring (issue #573 AC5)", () => {
  it("ChatShell.tsx references ShareAction", () => {
    const source = readFileSync(CHAT_SHELL_PATH, "utf-8");

    expect(
      source.includes("ShareAction"),
      "ChatShell.tsx must reference ShareAction so the share action is reachable from the chat header (issue #573 AC5 wiring)"
    ).toBe(true);
  });
});
