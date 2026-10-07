// frontend/src/components/layout/PageShell.l05.test.tsx
// Issue #776 L05 (frozen acceptance checks) — the PageShell content wrapper
// must reserve mobile bottom-nav clearance on chat routes, must NOT apply the
// chat wrapper to canvas routes, and must cap the non-chat content column
// with a max width.
//
// At master there is one wrapper branch (PageShell.tsx:100):
//   isChat ? "flex-1 min-h-0 overflow-hidden"
//          : "flex-1 min-h-0 p-6 lg:p-8 overflow-auto pb-20 md:pb-6 mx-auto w-full"
// with isChat = location.pathname.startsWith("/chat") (PageShell.tsx:47), so:
//   - the chat wrapper has no pb-* bottom-nav clearance below md,
//   - /chat/<id>/canvas/* inherits the same chat wrapper (overflow-hidden),
//   - the non-chat wrapper has no max-w-* cap.
//
// Expected RED at master:
//   "chat wrapper reserves bottom-nav clearance below md" — expected false to be true
//   "canvas route does not inherit the chat wrapper"     — expected true to be false
//   "non-chat content wrapper has a max width"           — expected false to be true
//
// Harness: the mock set from PageShell.test.tsx (api settings, onboarding,
// Navigation, UploadIndicator, framer-motion), re-pointed at per-route
// MemoryRouter initial entries. jsdom has no layout engine — all assertions
// are class-token only.

import type { HTMLAttributes, ReactNode } from "react";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PageShell } from "./PageShell";

const { mockGetSettings } = vi.hoisted(() => ({
  mockGetSettings: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getSettings: mockGetSettings,
}));

// Keep the FirstRunChecklist fetch deterministic (server says hidden) — the
// checklist renders null while pending, leaving the wrapper as the only
// element child of #main-content.
vi.mock("@/lib/api/onboarding", () => ({
  getOnboardingMilestones: vi.fn().mockResolvedValue({
    vault_created: true,
    upload_indexed: true,
    first_question_asked: true,
    first_citation_opened: true,
    show_checklist: false,
  }),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

vi.mock("./Navigation", () => ({
  Navigation: () => <nav aria-label="Primary navigation" />,
}));

vi.mock("@/components/shared/UploadIndicator", () => ({
  UploadIndicator: () => null,
}));

vi.mock("framer-motion", () => ({
  AnimatePresence: ({ children }: { children: ReactNode }) => <>{children}</>,
  motion: {
    div: ({
      children,
      variants: _variants,
      initial: _initial,
      animate: _animate,
      exit: _exit,
      transition: _transition,
      ...props
    }: HTMLAttributes<HTMLDivElement> & Record<string, unknown>) => (
      <div {...props}>{children}</div>
    ),
  },
  useReducedMotion: () => true,
}));

const shellProps = {
  activeItem: "documents" as const,
  onItemSelect: vi.fn(),
  healthStatus: {
    backend: true,
    embeddings: true,
    chat: true,
    loading: false,
    lastChecked: null,
  },
};

// Default: a configured system (banner hidden).
beforeEach(() => {
  mockGetSettings.mockResolvedValue({ chat_configured: true });
  sessionStorage.removeItem("unconfigured-chat-banner-dismissed");
});

function renderShellAt(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <PageShell {...shellProps}>
        <button type="button">Page content</button>
      </PageShell>
    </MemoryRouter>
  );
}

/** The layout wrapper: the direct child div of #main-content whose class
 *  list contains flex-1 (the PageShell.tsx:100 wrapper). */
function getLayoutWrapper(): HTMLElement {
  const main = document.getElementById("main-content");
  if (!main) throw new Error("#main-content not found");
  const wrapper = Array.from(main.children).find(
    (el): el is HTMLElement =>
      el instanceof HTMLElement && el.classList.contains("flex-1")
  );
  if (!wrapper) {
    throw new Error("layout wrapper (direct div child of #main-content with flex-1) not found");
  }
  return wrapper;
}

describe("PageShell L05 (issue #776)", () => {
  it("chat wrapper reserves bottom-nav clearance below md", () => {
    renderShellAt("/chat");

    const wrapper = getLayoutWrapper();
    // Some non-zero pb-* utility must apply below the md breakpoint
    // (pb-0 explicitly does not count).
    expect(/(^|\s)pb-(?!0(\s|$))\S+/.test(wrapper.className)).toBe(true);
  });

  it("canvas route does not inherit the chat wrapper", () => {
    renderShellAt("/chat/1/canvas/abc");

    const wrapper = getLayoutWrapper();
    expect(wrapper.className.split(/\s+/).includes("overflow-hidden")).toBe(false);
  });

  it("non-chat content wrapper has a max width", () => {
    renderShellAt("/settings");

    const wrapper = getLayoutWrapper();
    expect(/(^|\s)max-w-/.test(wrapper.className)).toBe(true);
  });
});
