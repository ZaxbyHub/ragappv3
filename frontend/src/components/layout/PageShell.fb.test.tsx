// frontend/src/components/layout/PageShell.fb.test.tsx
// Feedback round 853-20261005 (PR #853 review, PRR-044): the frozen
// PageShell.m04.test.tsx unmounts between routes instead of exercising a
// client-side navigation, so a tray double-mount during an in-app route
// change was unproven. This unfrozen file drives a real router navigation
// and asserts the Activity landmark exists exactly once throughout.

import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useNavigate, Link } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { PageShell } from "./PageShell";

const { mockGetSettings } = vi.hoisted(() => ({
  mockGetSettings: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getSettings: mockGetSettings,
}));

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

vi.mock("@/hooks/useJobStatus", () => ({
  useActivityJobs: () => ({ rows: [], loading: false, refresh: vi.fn() }),
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
    div: ({ children, ...props }: { children?: ReactNode }) => <div {...props}>{children}</div>,
  },
  useReducedMotion: () => true,
}));

const healthStatus = {
  backend: true,
  embeddings: true,
  chat: true,
  loading: false,
  lastChecked: null,
} as const;

function NavToChat() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/chat")}>
      navigate-to-chat
    </button>
  );
}

describe("PageShell Activity tray during client-side navigation (PRR-044)", () => {
  it("keeps exactly one Activity landmark across an in-app route change", async () => {
    mockGetSettings.mockResolvedValue({ chat_configured: true });
    sessionStorage.removeItem("unconfigured-chat-banner-dismissed");

    render(
      <MemoryRouter initialEntries={["/documents"]}>
        <PageShell activeItem="documents" onItemSelect={vi.fn()} healthStatus={healthStatus}>
          <Routes>
            <Route
              path="/documents"
              element={
                <div>
                  <button type="button">Page content</button>
                  <Link to="/chat">to-chat-link</Link>
                  <NavToChat />
                </div>
              }
            />
            <Route
              path="/chat"
              element={<button type="button">Chat content</button>}
            />
          </Routes>
        </PageShell>
      </MemoryRouter>
    );

    expect(screen.queryAllByRole("region", { name: /activity/i }).length).toBe(1);
    // Client-side navigation (no unmount/remount of the shell): the tray
    // must survive as exactly one landmark, not double-mount.
    await screen.findByText("Page content");
    screen.getByText("navigate-to-chat").click();
    await screen.findByText("Chat content");
    expect(screen.queryAllByRole("region", { name: /activity/i }).length).toBe(1);
  });
});
