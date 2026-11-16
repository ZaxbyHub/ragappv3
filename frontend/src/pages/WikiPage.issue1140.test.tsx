/**
 * PR #1140 (final-critic R3-B2): the WikiPage "Run lint" action must
 * surface a failed lint run (toast.error) instead of leaving an unhandled
 * rejection with no feedback — the backend can now answer a retryable 503
 * when the lint admission bound is saturated.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";

vi.mock("@/lib/api", () => ({
  listWikiPages: vi.fn().mockResolvedValue({ pages: [], page: 1, per_page: 50 }),
  getWikiPage: vi.fn(),
  createWikiPage: vi.fn(),
  updateWikiPage: vi.fn(),
  deleteWikiPage: vi.fn(),
  listWikiEntities: vi.fn().mockResolvedValue({ entities: [] }),
  listWikiClaims: vi.fn().mockResolvedValue({ claims: [] }),
  listWikiLintFindings: vi.fn().mockResolvedValue({ findings: [] }),
  runWikiLint: vi.fn(),
  searchWiki: vi.fn().mockResolvedValue({ pages: [], claims: [], entities: [], query: "" }),
  promoteMemoryToWiki: vi.fn(),
  updateMemory: vi.fn(),
  API_BASE_URL: "/api",
  getJwtAccessToken: vi.fn(() => null),
  refreshAccessToken: vi.fn(),
  getWikiActivityFeed: vi.fn().mockResolvedValue({ activity: [] }),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: () => ({ activeVaultId: 1 }),
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector">VaultSelector</div>,
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

import WikiPage from "./WikiPage";
import { runWikiLint } from "@/lib/api";
import { toast } from "sonner";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("issue 1140 WikiPage lint-run failure surfacing", () => {
  it("toasts the error when runWikiLint rejects (lint 503)", async () => {
    vi.mocked(runWikiLint).mockRejectedValueOnce(
      new Error("Wiki lint scan capacity reached; retry shortly")
    );

    render(<WikiPage />);

    // Open the lint panel first (the page-level toggle), then the panel's
    // Run Lint action.
    const toggle = await screen.findByRole("button", { name: /^Lint/i });
    fireEvent.click(toggle);
    const btn = await screen.findByRole("button", { name: /run lint/i });
    fireEvent.click(btn);

    await waitFor(() => expect(runWikiLint).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Wiki lint scan capacity reached; retry shortly"
      )
    );
    expect(toast.info).not.toHaveBeenCalled();
  });
});
