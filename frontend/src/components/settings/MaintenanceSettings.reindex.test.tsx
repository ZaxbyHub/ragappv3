/**
 * G04 (issues #736/#833) acceptance checks — Maintenance-surface reindex
 * trigger and recovery copy (AC8 + AC9).
 *
 * NEW-SURFACE: at the pre-fix base the Maintenance tab has no admin reindex
 * trigger at all (see the MaintenanceSettings.tsx header comment: "wiring a
 * maintenance-tab button for it is tracked separately") and the reindex-
 * required banner on SettingsPage points users at "Re-process documents
 * from the Documents page ... or run a wiki recompile" instead of the
 * reindex action. These are honest specs for the MISSING behavior: they are
 * expected to FAIL at base with meaningful assertion failures.
 *
 * - AC8a: a confirmed admin reindex trigger on the Maintenance settings
 *   surface issues a POST to /documents/reindex in the ALL-VAULT shape
 *   (the backend ReindexRequest model takes an optional vault_id; all-vault
 *   means no vault scope — body absent/empty or vault_id null).
 * - AC8b: the copy states that recovery from an embedding-mismatch 503
 *   requires an ALL-VAULT reindex run to completion and that restarting
 *   does not clear it.
 * - AC9c: the reindex-required guidance (SettingsPage banner) points at the
 *   reindex action and no longer tells the user to "Re-process documents
 *   from the Documents page" as the recovery path.
 *
 * Transport-level assertion: the REAL "@/lib/api" helpers run (partial mock
 * only replaces getSettings/updateSettings/testConnections for the
 * SettingsPage harness) and the REAL apiClient axios instance is stubbed at
 * the adapter boundary, so whatever helper the fix adds, its HTTP request
 * is observable. SettingsPage harness mirrors SettingsPage.k21-shellfill.
 * test.tsx (real page + real useSettingsStore + jsdom-friendly Tabs stub).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import "@testing-library/jest-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { SettingsResponse, UpdateSettingsRequest } from "@/lib/api";
import { mockSettings } from "@/fixtures/settings";
import { useSettingsStore } from "@/stores/useSettingsStore";

const { mockGetSettings, mockUpdateSettings, mockTestConnections } =
  vi.hoisted(() => ({
    mockGetSettings: vi.fn(),
    mockUpdateSettings: vi.fn(),
    mockTestConnections: vi.fn(),
  }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getSettings: mockGetSettings,
    updateSettings: mockUpdateSettings,
    testConnections: mockTestConnections,
  };
});

// SettingsPage mounts useHealthCheck, which hits the real apiClient GET
// /health. Stub the hook (same pattern as SettingsPage.k21-shellfill).
vi.mock("@/hooks/useHealthCheck", () => ({
  useHealthCheck: () => ({
    backend: true,
    embeddings: true,
    chat: true,
    loading: false,
    lastChecked: null,
  }),
}));

// Radix Tabs cannot be activated via fireEvent.click in jsdom. Mock the
// primitive to a plain controlled button/div pair (k21-shellfill pattern).
vi.mock("@/components/ui/tabs", async () => {
  const ReactMod = await import("react");
  const ValueCtx = ReactMod.createContext<string>("");
  const ChangeCtx = ReactMod.createContext<(v: string) => void>(() => {});
  return {
    Tabs: ({
      value,
      onValueChange,
      children,
    }: {
      value: string;
      onValueChange: (v: string) => void;
      children: React.ReactNode;
    }) =>
      ReactMod.createElement(
        ValueCtx.Provider,
        { value },
        ReactMod.createElement(ChangeCtx.Provider, { value: onValueChange }, children),
      ),
    TabsList: ({ children }: { children: React.ReactNode }) =>
      ReactMod.createElement("div", null, children),
    TabsTrigger: ({
      value,
      children,
    }: {
      value: string;
      children: React.ReactNode;
    }) => {
      const onValueChange = ReactMod.useContext(ChangeCtx);
      return ReactMod.createElement(
        "button",
        { role: "tab", onClick: () => onValueChange(value) },
        children,
      );
    },
    TabsContent: ({
      value,
      children,
    }: {
      value: string;
      children: React.ReactNode;
    }) => {
      const active = ReactMod.useContext(ValueCtx);
      return active === value ? ReactMod.createElement("div", null, children) : null;
    },
  };
});

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}));

import { apiClient, CSRF_COOKIE_NAME,
} from "@/lib/api";
import { MaintenanceSettings } from "./MaintenanceSettings";
import SettingsPage from "@/pages/SettingsPage";

// ---------------------------------------------------------------------------
// Transport recording: stub the REAL apiClient at the axios adapter boundary
// so every request the surface makes is observable, whatever helper name the
// implementation uses.
// ---------------------------------------------------------------------------

interface RecordedCall {
  method: string;
  url: string;
  data?: unknown;
}

const recorded: RecordedCall[] = [];
let originalAdapter: unknown;

function respondFor(url: string): unknown {
  if (url.includes("/wiki/jobs")) {
    return { jobs: [] };
  }
  if (url.includes("/documents/reindex")) {
    return { job_id: 1, status: "pending", vault_id: null };
  }
  return {};
}

beforeEach(() => {
  recorded.length = 0;
  // Seed the CSRF cookie the apiClient interceptor honors (CHECK_WRONG
  // amendment, issue #736/#833): the bare-fetch acquisition path
  // (fetch(`${API_BASE_URL}/csrf-token`)) cannot run in jsdom — Node's
  // fetch rejects the relative URL — so without a cookie every POST dies
  // in the interceptor before reaching the adapter boundary this check
  // observes. The cookie satisfies ensureCsrfToken's cookie-first branch;
  // the POST itself still flows through the real apiClient + adapter.
  document.cookie = `${CSRF_COOKIE_NAME}=g04-test-csrf-token; path=/`;
   
  const defaults = apiClient.defaults as any;
  originalAdapter = defaults.adapter;
  defaults.adapter = async (config: {
    method?: string;
    url?: string;
     
    data?: any;
     
  }): Promise<any> => {
    recorded.push({
      method: (config.method ?? "get").toLowerCase(),
      url: String(config.url ?? ""),
      data: config.data,
    });
    return {
      data: respondFor(String(config.url ?? "")),
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    };
  };

  mockGetSettings.mockReset().mockResolvedValue(mockSettings);
  mockTestConnections.mockReset().mockResolvedValue({});
  mockUpdateSettings.mockReset().mockImplementation(
    async (payload: UpdateSettingsRequest): Promise<SettingsResponse> => ({
      ...mockSettings,
      ...payload,
    }),
  );
  useSettingsStore.getState().resetState();
});

afterEach(() => {
   
  (apiClient.defaults as any).adapter = originalAdapter;
  document.cookie =
    `${CSRF_COOKIE_NAME}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
  cleanup();
});

/** Click through any confirmation dialog the surface shows before firing. */
async function clickThroughAnyConfirmation(): Promise<void> {
  for (let attempt = 0; attempt < 5; attempt++) {
    const dialog =
      screen.queryByRole("dialog") ?? screen.queryByRole("alertdialog");
    if (!dialog) return;
    const buttons = within(dialog).getAllByRole("button");
    const confirmButton = buttons.find(
      (button) =>
        /(confirm|re-?index|acknowledge|run|start|continue|yes|proceed|ok)/i.test(
          button.textContent ?? "",
        ) && !button.hasAttribute("disabled"),
    );
    if (!confirmButton) return;
    await act(async () => {
      fireEvent.click(confirmButton);
    });
  }
}

function reindexPosts(): RecordedCall[] {
  return recorded.filter(
    (call) =>
      call.method === "post" && call.url.includes("/documents/reindex"),
  );
}

describe("G04 AC8a: Maintenance admin reindex trigger issues an all-vault POST /documents/reindex", () => {
  it("renders a reindex trigger which, when confirmed, POSTs /documents/reindex without a vault scope", async () => {
    render(<MaintenanceSettings vaultId={1} />);

    // The Maintenance surface must render an admin reindex trigger. At the
    // pre-fix base no button matches — this is the NEW-SURFACE discriminator.
    const trigger = await screen.findByRole("button", { name: /re-?index/i });
    expect(trigger).toBeInTheDocument();

    fireEvent.click(trigger);
    await clickThroughAnyConfirmation();

    await waitFor(() => {
      expect(reindexPosts().length).toBeGreaterThan(0);
    }, undefined);

    const post = reindexPosts()[0];
    // All-vault shape per the backend ReindexRequest model (POST
    // /documents/reindex with optional vault_id): body absent/empty or
    // vault_id null/absent — never a truthy vault scope.
    let body: unknown = post.data;
    if (typeof body === "string" && body.trim() !== "") {
      body = JSON.parse(body);
    }
    const vaultScoped =
      body != null &&
      typeof body === "object" &&
      Boolean((body as { vault_id?: unknown }).vault_id);
    expect(
      vaultScoped,
      `AC8a: POST /documents/reindex must be all-vault (no vault_id); got body ${JSON.stringify(post.data)}`,
    ).toBe(false);
  });
});

describe("G04 AC8b: Maintenance copy states the all-vault reindex recovery contract", () => {
  it("says recovery requires an all-vault reindex run to completion and that restarting does not clear it", async () => {
    const { container } = render(<MaintenanceSettings vaultId={1} />);

    await waitFor(() => {
      expect(screen.getByText(/maintenance actions/i)).toBeInTheDocument();
    });

    const text = container.textContent ?? "";
    expect(
      text,
      "AC8b: the Maintenance copy must name an ALL-VAULT reindex as the recovery path",
    ).toMatch(/all[- ]vaults?/i);
    expect(
      text,
      "AC8b: the copy must say the reindex runs TO COMPLETION (a partial run does not recover)",
    ).toMatch(/to completion|until (it |the )?(re-?index )?completes/i);
    expect(
      text,
      "AC8b: the copy must say restarting does not clear the 503",
    ).toMatch(/restart/i);
    expect(
      text,
      "AC8b: the copy must state the state is NOT cleared by a restart",
    ).toMatch(/(does not|doesn't|won't|not)[^.]*clear|persists?/i);
  });
});

describe("G04 AC9c: reindex-required guidance points at the reindex action", () => {
  function renderSettingsPage() {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <SettingsPage />
      </QueryClientProvider>,
    );
  }

  it("banner no longer says 'Re-process documents from the Documents page' and points at the reindex action", async () => {
    // The banner renders when the settings store's reindexRequired flag is
    // set (the post-save state SettingsPage itself produces).
    act(() => {
      useSettingsStore.getState().setReindexRequired(true);
    });
    renderSettingsPage();

    const heading = await waitFor(() => {
      const node = screen.getByText("Reindex required");
      expect(node).toBeInTheDocument();
      return node;
    });
    const banner = heading.closest("div");
    const bannerText = banner?.textContent ?? "";

    expect(
      bannerText,
      "AC9c: the reindex-required banner must not send users to 'Re-process documents from the Documents page' as the recovery path",
    ).not.toMatch(/Re-process documents from the Documents page/i);
    expect(
      bannerText,
      "AC9c: the banner must point at the reindex action instead",
    ).toMatch(/re-?index/i);
  });
});
