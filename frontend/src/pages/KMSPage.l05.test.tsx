// frontend/src/pages/KMSPage.l05.test.tsx
// Issue #776 L05 / AC5 (frozen acceptance check) — every flex row between a
// header action button and the header root must be allowed to wrap on narrow
// viewports (flex-wrap or flex-col), so header actions reflow instead of
// overflowing.
//
// The check locates the "Recompile" header button, finds the header root
// (the nearest ancestor containing a descendant h1 — KMSPage.tsx:217, whose
// h1 is the "Knowledge Management" title at KMSPage.tsx:220), and walks the
// ancestor chain up to AND INCLUDING that root counting rows that are flex,
// not flex-col and not flex-wrap. At master TWO rows qualify: the action
// group "flex items-center gap-2" (KMSPage.tsx:223) AND the header root
// itself ("flex items-center justify-between px-6 py-4 ...").
//
// Expected RED at master: "expected 2 to be +0".
//
// Harness: the mock set from KMSPage.m03.test.tsx / KMSPage.m02.test.tsx
// (api factory mock incl. listKMSJobs/getKMSJob, useVaultStore with an
// active vault, VaultSelector data-testid stub, sonner, MemoryRouter —
// KMSPage calls useNavigate). jsdom has no layout engine — the assertion is
// class-token only.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render as rtlRender, screen, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

vi.mock("@/lib/api", () => ({
  listKMSEntries: vi
    .fn()
    .mockResolvedValue({ entries: [], total: 0, page: 1, per_page: 200 }),
  createKMSEntry: vi.fn(),
  recompileVaultKMS: vi.fn().mockResolvedValue({ job_id: 7, status: "pending" }),
  listKMSJobs: vi.fn().mockResolvedValue({ jobs: [] }),
  // The factory defines the module surface: getKMSJob is not exported today,
  // but a post-fix client resolves here (same note as KMSPage.m03.test.tsx).
  getKMSJob: vi.fn().mockResolvedValue(null),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: () => ({ activeVaultId: 1 }),
}));

// Same data-testid stub as the m01/m02/m03 fixtures.
vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

import KMSPage from "@/pages/KMSPage";

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

describe("KMSPage L05 (issue #776)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("header action rows wrap", async () => {
    await act(async () => {
      render(<KMSPage />);
    });

    const button = screen.getByRole("button", { name: /recompile/i });

    // Header root = the nearest ancestor of the button that contains a
    // descendant h1 (KMSPage's own "Knowledge Management" h1).
    let headerRoot: HTMLElement | null = null;
    for (
      let el: HTMLElement | null = button.parentElement;
      el instanceof HTMLElement;
      el = el.parentElement
    ) {
      if (el.querySelector("h1")) {
        headerRoot = el;
        break;
      }
    }
    expect(headerRoot).not.toBeNull();

    // Walk from the button's ancestors up to AND INCLUDING the header root;
    // count flex rows that are neither flex-col nor flex-wrap.
    let unwrappedRows = 0;
    const stop = (headerRoot as HTMLElement).parentElement;
    for (
      let el: HTMLElement | null = button.parentElement;
      el instanceof HTMLElement && el !== stop;
      el = el.parentElement
    ) {
      const cls = el.getAttribute("class") ?? "";
      if (cls.includes("flex") && !cls.includes("flex-col") && !cls.includes("flex-wrap")) {
        unwrappedRows += 1;
      }
    }

    expect(unwrappedRows).toBe(0);
  });
});
