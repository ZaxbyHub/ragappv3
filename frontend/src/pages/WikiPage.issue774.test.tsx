/**
 * Issue #774 regression test (non-frozen): the WikiPage activity panel must
 * render a failure line when the activity fetch rejects, not "No recent
 * activity." — a failed load is not a verdict that nothing happened.
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
  runWikiLint: vi.fn().mockResolvedValue({ findings: [], count: 0 }),
  searchWiki: vi.fn().mockResolvedValue({ pages: [], claims: [], entities: [], query: "" }),
  promoteMemoryToWiki: vi.fn(),
  updateMemory: vi.fn(),
  API_BASE_URL: "/api",
  getJwtAccessToken: vi.fn(() => null),
  refreshAccessToken: vi.fn(),
  getWikiActivityFeed: vi.fn().mockRejectedValue(new Error("activity down")),
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
import { getWikiActivityFeed } from "@/lib/api";

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getWikiActivityFeed).mockRejectedValue(new Error("activity down"));
});

describe("issue 774 WikiPage activity failure state", () => {
  it("renders a failure line, not the empty verdict, when the activity feed fails to load", async () => {
    render(<WikiPage />);

    fireEvent.click(screen.getByRole("button", { name: /activity/i }));

    await waitFor(() => expect(getWikiActivityFeed).toHaveBeenCalled());
    const alert = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(alert).toHaveTextContent(/failed to load activity/i);
    expect(screen.queryByText("No recent activity.")).not.toBeInTheDocument();
  });
});
