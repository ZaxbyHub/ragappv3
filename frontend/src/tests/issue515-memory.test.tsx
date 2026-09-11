/**
 * Issue #515 acceptance checks — memory add flow and the unified discovery
 * surface.
 *
 * AC41 — Ctrl+Enter submits exactly once while the add request is in flight
 *        (useMemoryCrud.handleAddMemory has no in-flight guard today).
 * AC28 — the add path surfaces feedback for EMPTY content instead of a
 *        silent no-op.
 * AC45 — a unified/global search surface exists in the app shell: an
 *        accessible searchbox, submitting navigates to /search?q=..., and a
 *        result-type filter control is present. (The unified API contract is
 *        proven by the backend pytest half of this check.)
 *
 * All tests are DISCRIMINATING: they fail on the current tree.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import React from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { toast } from "sonner";

// ---------------------------------------------------------------------------
// MemoryPage mocks — real MemoryPage + real useMemoryCrud; only the data
// edges (api barrel, search hook, vault store) are mocked.
// ---------------------------------------------------------------------------
vi.mock("@/lib/api", () => ({
  addMemory: vi.fn(),
  deleteMemory: vi.fn(),
  updateMemory: vi.fn(),
  promoteMemoryToWiki: vi.fn(),
  getMemoryWikiStatus: vi.fn(),
  batchMemoryWikiStatus: vi.fn().mockResolvedValue({}),
}));

vi.mock("@/hooks/useMemorySearch", () => ({
  useMemorySearch: vi.fn(() => ({
    memories: [],
    searchQuery: "",
    setSearchQuery: vi.fn(),
    loading: false,
    handleSearch: vi.fn().mockResolvedValue(undefined),
  })),
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

// ---------------------------------------------------------------------------
// App-shell mocks for AC45 (NavigationRail.test.tsx patterns) — the REAL
// Navigation tree stays mounted so a shell-level search control renders.
// ---------------------------------------------------------------------------
const mockLogout = vi.hoisted(() => vi.fn());

vi.mock("@/stores/useThemeStore", () => ({
  useThemeStore: vi.fn(() => ({ theme: "dark", setTheme: vi.fn() })),
  applyTheme: vi.fn(),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(
    (selector: (s: { user: { role: string } | null; logout: () => Promise<void> }) => unknown) =>
      selector({ user: { role: "admin" }, logout: mockLogout })
  ),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: vi.fn(),
  useDraftRoomVisible: vi.fn(() => false),
}));

vi.mock("@/components/shared/UploadIndicator", () => ({
  UploadIndicator: () => null,
}));

import MemoryPage from "@/pages/MemoryPage";
import { addMemory } from "@/lib/api";
import { PageShell } from "@/components/layout/PageShell";

const addMock = vi.mocked(addMemory);

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockLogout.mockResolvedValue(undefined);
});

const flush = () => act(async () => {});

function openAddMemoryForm() {
  render(<MemoryPage />);
  // Header action (unique before the form's own submit button appears).
  fireEvent.click(screen.getByRole("button", { name: /add memory/i }));
  // The form card is up once the content textarea is labelled.
  const textarea = screen.getByLabelText(/^content \*$/i) as HTMLTextAreaElement;
  return { textarea };
}

// ---------------------------------------------------------------------------
// AC41 — Ctrl+Enter single submit
// ---------------------------------------------------------------------------
describe("issue515 ac41", () => {
  it("issue515-ac41 ctrl+enter during in-flight add submits exactly once", async () => {
    const pending = deferred<unknown>();
    addMock.mockReturnValue(pending.promise as any);

    const { textarea } = openAddMemoryForm();
    fireEvent.change(textarea, { target: { value: "Ctrl+Enter must submit once" } });

    // Fire the keyboard shortcut twice while the first request is in flight.
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    await flush();
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    await flush();

    expect(addMock).toHaveBeenCalledTimes(1);

    // Resolving the add clears the submitting state: success toast fires and
    // the add form closes (dialog reset).
    await act(async () => {
      pending.resolve({ id: "m-1" });
    });
    await flush();
    expect(toast.success).toHaveBeenCalled();
    expect(screen.queryByText("Add New Memory")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// AC28 — add path surfaces feedback for empty content
// ---------------------------------------------------------------------------
describe("issue515 ac28", () => {
  it("issue515-ac28 add path surfaces feedback", async () => {
    const { textarea } = openAddMemoryForm();
    // Content stays EMPTY (the submit button is disabled in this state, so
    // the keyboard path is the reachable trigger).
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    await flush();

    // User-visible feedback is REQUIRED: an alert region or a toast that
    // says the content cannot be empty.
    const alertEl = screen.queryByRole("alert");
    const toastMentionsEmpty = (toast.error as ReturnType<typeof vi.fn>).mock.calls.some(
      (call) => /empty/i.test(String(call[0] ?? ""))
    );
    expect(alertEl !== null || toastMentionsEmpty).toBe(true);

    // And no request may have been sent.
    expect(addMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// AC45 — unified discovery surface
// ---------------------------------------------------------------------------
describe("issue515 ac45", () => {
  it("issue515-ac45 unified discovery surface", async () => {
    function LocationProbe() {
      const loc = useLocation();
      return <div data-testid="location-probe">{loc.pathname}{loc.search}</div>;
    }

    render(
      <MemoryRouter initialEntries={["/wiki"]}>
        <PageShell
          activeItem="wiki"
          onItemSelect={() => {}}
          healthStatus={{
            backend: true,
            embeddings: true,
            chat: true,
            loading: false,
            lastChecked: null,
          }}
        >
          <LocationProbe />
        </PageShell>
      </MemoryRouter>
    );

    // The shell/side rail exposes a global searchbox whose accessible name
    // says it searches across surfaces.
    const searchbox = screen.queryByRole("searchbox", {
      name: /search (across|everything|all)/i,
    });
    expect(
      searchbox,
      "app shell must expose a global searchbox (accessible name like 'Search across…')"
    ).toBeTruthy();

    // Submitting a query routes to the unified search surface with the q param.
    fireEvent.change(searchbox!, { target: { value: "visa" } });
    fireEvent.keyDown(searchbox!, { key: "Enter" });
    await waitFor(() => {
      expect(screen.getByTestId("location-probe").textContent ?? "").toMatch(/\/search/);
    });
    expect(screen.getByTestId("location-probe").textContent ?? "").toContain("q=visa");

    // A result-type filter control is present (combobox or per-type
    // checkboxes for document/wiki/kms/chat results).
    const combo = screen.queryByRole("combobox", { name: /type|filter|scope/i });
    const typeCheckboxes = screen
      .queryAllByRole("checkbox")
      .filter((el) => /document|wiki|kms|chat/i.test(el.getAttribute("aria-label") ?? ""));
    expect(combo !== null || typeCheckboxes.length >= 3).toBe(true);
  });
});
