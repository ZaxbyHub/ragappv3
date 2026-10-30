/**
 * PageShell K05 acceptance check (issue #768 AC7).
 *
 * WHEN the authenticated shell loads, the settings fetch it already performs
 * must populate the settings store, so the server's max_file_size_mb (and the
 * upload allowlist) reach upload surfaces without the Settings page ever
 * being opened. At base PageShell keeps only chat_configured in local state
 * and the store stays null. RED at base, GREEN after the fix.
 *
 * Harness mirrors PageShell.test.tsx (proven in this repo); the settings
 * store is the REAL store, reset before the check.
 */
import type { HTMLAttributes, ReactNode } from "react";
import { render, screen, waitFor, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { PageShell } from "./PageShell";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { mockSettings } from "@/fixtures/settings";

const { mockGetSettings } = vi.hoisted(() => ({
  mockGetSettings: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getSettings: mockGetSettings,
}));

// Issue #782: PageShell now mounts the FirstRunChecklist; keep its
// fetch deterministic here (server says hidden).
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

function renderShell() {
  return render(
    <MemoryRouter initialEntries={["/documents"]}>
      <PageShell {...shellProps}>
        <button type="button">Page content</button>
      </PageShell>
    </MemoryRouter>
  );
}

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

describe("PageShell K05 settings store population (issue #768)", () => {
  beforeEach(() => {
    mockGetSettings.mockReset();
    sessionStorage.removeItem("unconfigured-chat-banner-dismissed");
    useSettingsStore.getState().resetState();
  });

  afterEach(() => {
    useSettingsStore.getState().resetState();
  });

  it("shell settings fetch populates the settings store", async () => {
    mockGetSettings.mockResolvedValue({
      ...mockSettings,
      chat_configured: true,
      max_file_size_mb: 250,
    });

    renderShell();

    await waitFor(() => {
      expect(mockGetSettings).toHaveBeenCalled();
    });
    await act(async () => {
      await flushMicrotasks();
    });

    // The shell already fetched /settings; that fetch must also land in the
    // settings store so upload surfaces see the server's limit without the
    // Settings page being opened. RED at base (store stays null), GREEN
    // after the fix.
    expect(useSettingsStore.getState().settings?.max_file_size_mb).toBe(250);
  });
});
