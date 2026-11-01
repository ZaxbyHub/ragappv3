// frontend/src/components/layout/PageShell.m04.test.tsx
// Issue-trace 784-activity-tray-shell-job-center — acceptance check C1 (AC2).
//
// The Activity tray must be a SHELL surface, not a page surface: PageShell
// mounts it on every authenticated route, so background work stays visible
// from any page (the "job center" half of the issue title). This check
// renders the REAL PageShell (no ActivityTray import here — the landmark
// must arrive through the shell) on /documents and /chat and asserts exactly
// one `role="region"` landmark named /activity/i exists per render.
//
// Mock idioms mirrored from PageShell.test.tsx (same file, same module):
// @/lib/api getSettings factory, @/lib/api/onboarding hidden checklist,
// ./Navigation stub, UploadIndicator stub, framer-motion passthrough.
// Additionally @/hooks/useJobStatus is replaced wholesale with the future
// aggregation export (useActivityJobs) returning empty rows, so once the
// real tray mounts through the shell it renders deterministically here.
//
// Expected pre-fix (base) verdict:
//   C1 RED with "expected +0 to be 1" (no /activity/i region is rendered).

import type { HTMLAttributes, ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
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

// The future tray consumes the hook's aggregation export; the factory
// replaces the module wholesale for THIS file (the real module exists at
// base with the other exports — nothing in PageShell's graph needs them,
// only KMSPage imports useJobStatus and it is not mounted here).
vi.mock("@/hooks/useJobStatus", () => ({
  useActivityJobs: () => ({ rows: [], loading: false }),
}));

const healthStatus = {
  backend: true,
  embeddings: true,
  chat: true,
  loading: false,
  lastChecked: null,
} as const;

beforeEach(() => {
  mockGetSettings.mockResolvedValue({ chat_configured: true });
  sessionStorage.removeItem("unconfigured-chat-banner-dismissed");
});

describe("PageShell m04 (issue-trace 784-activity-tray-shell-job-center)", () => {
  it("shell mounts the Activity tray on every route", () => {
    // First route: /documents.
    const documentsView = render(
      <MemoryRouter initialEntries={["/documents"]}>
        <PageShell activeItem="documents" onItemSelect={vi.fn()} healthStatus={healthStatus}>
          <button type="button">Page content</button>
        </PageShell>
      </MemoryRouter>
    );
    expect(screen.queryAllByRole("region", { name: /activity/i }).length).toBe(1);

    // Unmount between routes: the second render must stand on its own.
    documentsView.unmount();

    // Second route: /chat (edge-to-edge layout branch of the shell).
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <PageShell activeItem="chat" onItemSelect={vi.fn()} healthStatus={healthStatus}>
          <button type="button">Page content</button>
        </PageShell>
      </MemoryRouter>
    );
    expect(screen.queryAllByRole("region", { name: /activity/i }).length).toBe(1);
  });
});
