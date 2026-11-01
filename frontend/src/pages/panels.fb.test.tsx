/**
 * Feedback-round behavior pins (re-gate finding 3) for the four panel fixes
 * that shipped without coverage — each test fails when its fix line is
 * removed:
 *   1. MaintenanceSettings: deselecting the vault clears a stale jobsError
 *      banner (PRR-004).
 *   2. OrgsPage: editing the member-search field retires the previous
 *      query's failure line (PRR-005 re-gate strengthening).
 *   3. OrgsPage: a successful create clears orgsError so the ErrorState
 *      cannot hide the new org (OOB F-008).
 *   4. WikiPageDetail: a superseded section fetch's rejection must not stamp
 *      versionsError over the current page's data (PRR-006).
 * (New unfrozen file; the frozen checkpoint manifest is byte-locked.)
 *
 * ONE unified "@/lib/api" mock (default + named exports) — multiple
 * vi.mock factories for the same module in one file silently override each
 * other (last factory wins at hoist).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";
import type { WikiPage } from "@/lib/api";

// ---------------------------------------------------------------------------
// Shared mocks — ONE factory for "@/lib/api"
// ---------------------------------------------------------------------------

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

// CardHeader must forward props (WikiPageDetail's section headers attach
// onClick to it; a children-only mock silently drops the toggle).
vi.mock("@/components/ui/card", () => ({
  Card: (props: React.HTMLAttributes<HTMLDivElement>) => <div data-testid="card" {...props} />,
  CardContent: (props: React.HTMLAttributes<HTMLDivElement>) => <div data-testid="card-content" {...props} />,
  CardHeader: (props: React.HTMLAttributes<HTMLDivElement>) => <div data-testid="card-header" {...props} />,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h3>{children}</h3>,
  CardDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, onClick, disabled, ...props }: { children: React.ReactNode; onClick?: () => void; disabled?: boolean }) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock("@/components/ui/badge", () => ({
  Badge: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) => (open ? <div data-testid="dialog">{children}</div> : null),
  DialogContent: ({ children }: { children: React.ReactNode }) => <div data-testid="dialog-content">{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

vi.mock("@/components/auth/RoleGuard", () => ({
  AdminGuard: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  RoleGuard: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const apiGet = vi.hoisted(() => vi.fn());
const apiPost = vi.hoisted(() => vi.fn());
const listWikiJobsMock = vi.hoisted(() => vi.fn());
const getWikiPageVersionsMock = vi.hoisted(() => vi.fn());
const getWikiPageFilesMock = vi.hoisted(() => vi.fn());
const getWikiPageBacklinksMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  default: {
    get: apiGet,
    post: apiPost,
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
  API_BASE_URL: "/api",
  getJwtAccessToken: vi.fn(() => null),
  refreshAccessToken: vi.fn(),
  listWikiJobs: listWikiJobsMock,
  recompileVaultWiki: vi.fn(),
  runWikiLint: vi.fn(),
  testConnections: vi.fn(),
  getWikiPageVersions: getWikiPageVersionsMock,
  getWikiPageFiles: getWikiPageFilesMock,
  getWikiPageBacklinks: getWikiPageBacklinksMock,
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn((selector: unknown) => {
    const state = {
      user: { id: 1, username: "superadmin", full_name: "Super Admin", role: "superadmin" },
      isAuthenticated: true,
      isLoading: false,
    };
    return typeof selector === "function"
      ? (selector as (s: typeof state) => unknown)(state)
      : state;
  }),
}));

// ===========================================================================
// 1. MaintenanceSettings (PRR-004) — deselect clears the stale banner
// ===========================================================================

import { MaintenanceSettings } from "@/components/settings/MaintenanceSettings";

describe("MaintenanceSettings stale jobsError (PRR-004 feedback pin)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("clears the couldn't-load banner when the vault is deselected", async () => {
    // First load with a vault: the request fails -> destructive banner.
    listWikiJobsMock.mockRejectedValueOnce(new Error("down"));
    const { rerender } = render(<MaintenanceSettings vaultId={1} />);
    await waitFor(() => expect(listWikiJobsMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Couldn't load recent jobs")).toBeInTheDocument();

    // Deselect the vault: no request is made, but the banner must go.
    rerender(<MaintenanceSettings vaultId={null} />);

    await waitFor(() =>
      expect(screen.queryByText("Couldn't load recent jobs")).not.toBeInTheDocument()
    );
    expect(screen.getByText("No recent jobs.")).toBeInTheDocument();
  });
});

// ===========================================================================
// 2+3. OrgsPage (PRR-005 re-gate + OOB F-008 create residue)
// ===========================================================================

import OrgsPage from "@/pages/OrgsPage";

const ORG = {
  id: 1,
  name: "Acme",
  description: "",
  member_count: 0,
  vault_count: 0,
  created_at: "2024-01-01T00:00:00",
};

describe("OrgsPage feedback pins (PRR-005 re-gate + OOB F-008)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiPost.mockResolvedValue({ data: { ...ORG, id: 2, name: "Newco" } });
  });

  it("retires the member-search failure line as soon as the query is edited", async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (url === "/organizations/") return { data: { organizations: [ORG], total: 1 } };
      if (url === "/organizations/1/members") return { data: { members: [] } };
      if (url.startsWith("/users/")) throw new Error("directory down");
      return { data: {} };
    });

    render(<OrgsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /expand/i }, { timeout: 3000 }));
    const search = await screen.findByPlaceholderText(
      /Search by name or username/i,
      undefined,
      { timeout: 3000 }
    );
    fireEvent.change(search, { target: { value: "alice" } });
    expect(
      await screen.findByText(/user search failed/i, {}, { timeout: 4000 })
    ).toBeInTheDocument();

    // Editing the field retires the previous query's failure immediately
    // (before the debounced request for the new query settles).
    fireEvent.change(search, { target: { value: "al" } });
    expect(screen.queryByText(/user search failed/i)).not.toBeInTheDocument();
  });

  it("clears the orgs ErrorState after a successful create so the new org is visible", async () => {
    // Initial load FAILS -> ErrorState with the create dialog still
    // available above it.
    apiGet.mockImplementation(async (url: string) => {
      if (url === "/organizations/") throw new Error("orgs endpoint down");
      if (url.startsWith("/users/")) return { data: { users: [] } };
      return { data: {} };
    });
    render(<OrgsPage />);
    expect(
      await screen.findByText("No organizations found — couldn't load", {}, { timeout: 3000 })
    ).toBeInTheDocument();

    // Create an organization through the dialog form.
    fireEvent.click(screen.getByRole("button", { name: /create organization/i }));
    const dialog = await screen.findByTestId("dialog", {}, { timeout: 3000 });
    const nameInput = dialog.querySelector('input[aria-label="Organization name"]');
    expect(nameInput).toBeTruthy();
    fireEvent.change(nameInput!, { target: { value: "Newco" } });
    fireEvent.submit(nameInput!.closest("form")!);

    // The create succeeded: the new org card renders and the error state is
    // gone (without the fix, orgsError stays true and hides the list).
    expect(await screen.findByText("Newco", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(
      screen.queryByText("No organizations found — couldn't load")
    ).not.toBeInTheDocument();
  });
});

// ===========================================================================
// 4. WikiPageDetail (PRR-006) — superseded rejection must not stamp the flag
// ===========================================================================

import { WikiPageDetail } from "@/pages/WikiPageDetail";

function makePage(id: number, title: string): WikiPage {
  return {
    id,
    vault_id: 2,
    slug: `doc/${title}`,
    title,
    page_type: "entity",
    summary: "",
    markdown: "",
    status: "draft",
    confidence: 0,
    created_by: null,
    created_at: "2024-01-01T00:00:00",
    updated_at: "2024-01-01T00:00:00",
    last_compiled_at: null,
    claims: [],
    entities: [],
    lint_findings: [],
  } as unknown as WikiPage;
}

describe("WikiPageDetail superseded-fetch guard (PRR-006 feedback pin)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not stamp versionsError when a page switch supersedes a slow rejection", async () => {
    // Page A's versions fetch hangs; page B's resolves.
    let rejectA: (e: unknown) => void = () => undefined;
    getWikiPageVersionsMock.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectA = reject;
        })
    );
    getWikiPageVersionsMock.mockResolvedValueOnce([
      {
        id: 301,
        page_id: 20,
        vault_id: 2,
        title: "Page B",
        markdown: "# B",
        summary: "",
        status: "draft",
        confidence: 0.5,
        edited_by: null,
        created_at: "2024-02-02T00:00:00Z",
      },
    ]);
    getWikiPageFilesMock.mockResolvedValue([]);
    getWikiPageBacklinksMock.mockResolvedValue([]);

    const { rerender } = render(
      <MemoryRouter>
        <WikiPageDetail
          page={makePage(10, "Page A")}
          onBack={() => {}}
          onEdit={() => {}}
          onDelete={() => {}}
        />
      </MemoryRouter>
    );
    fireEvent.click(screen.getByText("Version History"));
    // Switch to page B while A's fetch is still pending.
    rerender(
      <MemoryRouter>
        <WikiPageDetail
          page={makePage(20, "Page B")}
          onBack={() => {}}
          onEdit={() => {}}
          onDelete={() => {}}
        />
      </MemoryRouter>
    );
    expect(await screen.findByText("v301", {}, { timeout: 3000 })).toBeInTheDocument();

    // NOW A's fetch rejects: the cancelled guard must drop it. Let the
    // rejection's microtasks land before asserting.
    rejectA(new Error("page A came back dead"));
    await waitFor(() => expect(getWikiPageVersionsMock).toHaveBeenCalledTimes(2));
    await new Promise((r) => setTimeout(r, 25));

    // B's data is still on screen and no failure line was stamped.
    expect(screen.getByText("v301")).toBeInTheDocument();
    expect(screen.queryByText("Failed to load version history.")).not.toBeInTheDocument();
  });
});
