// Issue #775 AC4–AC5/AC7: exercise the real app shell and CommandPalette
// navigation/search wiring. Routed pages are marker stubs only for these
// navigation checks; the real palette and router remain mounted.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useVaultStore } from "@/stores/useVaultStore";

const paletteHarness = vi.hoisted(() => ({
  role: "admin",
  principalId: 1,
  draftRoomVisible: true,
  unifiedSearch: vi.fn(),
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

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      user: { id: paletteHarness.principalId, username: "alice", role: paletteHarness.role },
      logout: vi.fn(),
      init: vi.fn().mockResolvedValue(undefined),
    }),
}));

vi.mock("@/components/auth/ProtectedRoute", () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@/hooks/useHealthCheck", () => ({
  useHealthCheck: () => ({ backend: true, embeddings: true, chat: true, loading: false }),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => paletteHarness.draftRoomVisible,
}));

vi.mock("@/lib/api/search", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/search")>();
  return { ...actual, unifiedSearch: paletteHarness.unifiedSearch };
});

// Keep the shell and router real while isolating unrelated page internals.
vi.mock("@/pages/ChatShell", () => ({ default: () => <div>Chat Page</div> }));
vi.mock("@/pages/DocumentsPage", () => ({ default: () => <div>Documents Page</div> }));
vi.mock("@/pages/DocumentDetailPage", () => ({ default: () => <div>Document Detail Page</div> }));
vi.mock("@/pages/MemoryPage", () => ({ default: () => <div>Memory Page</div> }));
vi.mock("@/pages/VaultsPage", () => ({ default: () => <div>Vaults Page</div> }));
vi.mock("@/pages/SettingsPage", () => ({ default: () => <div>Settings Page</div> }));
vi.mock("@/pages/LoginPage", () => ({ default: () => <div>Login Page</div> }));
vi.mock("@/pages/SetupPage", () => ({ default: () => <div>Setup Page</div> }));
vi.mock("@/pages/RegisterPage", () => ({ default: () => <div>Register Page</div> }));
vi.mock("@/pages/AdminUsersPage", () => ({ default: () => <div>Admin Users Page</div> }));
vi.mock("@/pages/AdminGroupsPage", () => ({ default: () => <div>Admin Groups Page</div> }));
vi.mock("@/pages/OrgsPage", () => ({ default: () => <div>Organizations Page</div> }));
vi.mock("@/pages/ProfilePage", () => ({ default: () => <div>Profile Page</div> }));
vi.mock("@/pages/ChangePasswordRequiredPage", () => ({ default: () => <div>Change Password Page</div> }));
vi.mock("@/pages/NotFoundPage", () => ({ default: () => <div>Not Found Page</div> }));
vi.mock("@/pages/WikiPage", () => ({ default: () => <div>Wiki Page</div> }));
vi.mock("@/pages/KMSPage", () => ({ default: () => <div>KMS Page</div> }));
vi.mock("@/pages/KMSDetailPage", () => ({ default: () => <div>KMS Detail Page</div> }));
vi.mock("@/pages/DraftRoomPage", () => ({ default: () => <div>Draft Room Page</div> }));
vi.mock("@/pages/DraftRoomDetailPage", () => ({ default: () => <div>Draft Room Detail Page</div> }));
vi.mock("@/pages/SearchPage", () => ({ default: () => <div>Search Page</div> }));

async function openPalette() {
  fireEvent.keyDown(document.body, { key: "k", code: "KeyK", ctrlKey: true });
  return screen.findByRole("dialog");
}

// Async-context supplement for AC7. The real App/router/CommandPalette remain
// mounted. Only the existing unifiedSearch transport boundary is deferred; no
// action owner or navigation implementation is replaced.
function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function searchResult(title: string, url_hint: string, id: number, vault_id = 1) {
  return {
    type: "document" as const,
    id,
    title,
    snippet: `${title} snippet`,
    vault_id,
    url_hint,
    score: 1,
  };
}

async function renderAsyncPalette() {
  window.history.pushState({}, "", "/vaults");
  const { default: App } = await import("../App");
  const view = render(<App />);
  expect(await screen.findByText("Vaults Page")).toBeInTheDocument();
  return { App, view };
}

async function typePaletteQuery(value: string) {
  const palette = await openPalette();
  fireEvent.change(within(palette).getByRole("textbox", { name: "Search commands" }), {
    target: { value },
  });
  return palette;
}

function resetAsyncContext() {
  paletteHarness.role = "admin";
  paletteHarness.principalId = 1;
  paletteHarness.draftRoomVisible = true;
  useVaultStore.setState({
    activeVaultId: null,
    vaults: [
      { id: 7, name: "Research", file_count: 0 },
      { id: 8, name: "Archive", file_count: 0 },
    ],
  });
  window.history.pushState({}, "", "/vaults");
}

describe("#775 palette async search context ownership", { timeout: 15_000 }, () => {
  beforeAll(async () => {
    await import("../App");
  }, 15_000);
  beforeEach(() => {
    vi.clearAllMocks();
    paletteHarness.unifiedSearch.mockReset();
    resetAsyncContext();
  });

  afterEach(() => {
    cleanup();
    useVaultStore.setState({ activeVaultId: null, vaults: [] });
  });

  it("ignores stale search results after query replacement while navigating the current hit by url_hint", async () => {
    const first = deferred<{ results: ReturnType<typeof searchResult>[] }>();
    const second = deferred<{ results: ReturnType<typeof searchResult>[] }>();
    paletteHarness.unifiedSearch.mockImplementation((request: { q?: string }) => {
      if (request.q === "alpha") return first.promise;
      if (request.q === "beta") return second.promise;
      return Promise.resolve({ results: [] });
    });
    await renderAsyncPalette();

    const palette = await typePaletteQuery("alpha");
    await waitFor(() =>
      expect(paletteHarness.unifiedSearch).toHaveBeenCalledWith(expect.objectContaining({ q: "alpha" }))
    );
    fireEvent.change(within(palette).getByRole("textbox", { name: "Search commands" }), {
      target: { value: "beta" },
    });
    await waitFor(() =>
      expect(paletteHarness.unifiedSearch).toHaveBeenCalledWith(expect.objectContaining({ q: "beta" }))
    );

    await act(async () => first.resolve({ results: [searchResult("Stale alpha", "/documents/41", 41)] }));
    await waitFor(() => expect(screen.queryByText("Stale alpha")).not.toBeInTheDocument());
    await act(async () => second.resolve({ results: [searchResult("Current beta", "/documents/42", 42)] }));
    const current = await within(await screen.findByRole("dialog")).findByText("Current beta");
    fireEvent.click(current);
    await waitFor(() => expect(window.location.pathname).toBe("/documents/42"));
  });

  it("retires a pending search when the palette closes and reopens", async () => {
    const first = deferred<{ results: ReturnType<typeof searchResult>[] }>();
    const second = deferred<{ results: ReturnType<typeof searchResult>[] }>();
    paletteHarness.unifiedSearch.mockImplementation((request: { q?: string }) => {
      if (request.q === "alpha") return first.promise;
      if (request.q === "beta") return second.promise;
      return Promise.resolve({ results: [] });
    });
    await renderAsyncPalette();

    await typePaletteQuery("alpha");
    await waitFor(() =>
      expect(paletteHarness.unifiedSearch).toHaveBeenCalledWith(expect.objectContaining({ q: "alpha" }))
    );
    fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const reopened = await typePaletteQuery("beta");
    await waitFor(() =>
      expect(paletteHarness.unifiedSearch).toHaveBeenCalledWith(expect.objectContaining({ q: "beta" }))
    );

    await act(async () => first.resolve({ results: [searchResult("Closed stale", "/documents/43", 43)] }));
    await waitFor(() => expect(screen.queryByText("Closed stale")).not.toBeInTheDocument());
    await act(async () => second.resolve({ results: [searchResult("Reopened current", "/documents/44", 44)] }));
    fireEvent.click(await within(reopened).findByText("Reopened current"));
    await waitFor(() => expect(window.location.pathname).toBe("/documents/44"));
  });

  it("drops a late result after vault replacement and navigates the current vault hit", async () => {
    const first = deferred<{ results: ReturnType<typeof searchResult>[] }>();
    const second = deferred<{ results: ReturnType<typeof searchResult>[] }>();
    paletteHarness.unifiedSearch.mockImplementation((request: { q?: string }) => {
      if (request.q === "alpha" && useVaultStore.getState().activeVaultId === 7) return first.promise;
      if (request.q === "alpha") return Promise.resolve({ results: [] });
      if (request.q === "beta") return second.promise;
      return Promise.resolve({ results: [] });
    });
    const { App, view } = await renderAsyncPalette();
    useVaultStore.setState({ activeVaultId: 7 });
    view.rerender(<App />);

    const palette = await typePaletteQuery("alpha");
    await waitFor(() =>
      expect(paletteHarness.unifiedSearch).toHaveBeenCalledWith(expect.objectContaining({ q: "alpha" }))
    );
    await act(async () => {
      useVaultStore.setState({ activeVaultId: 8 });
      view.rerender(<App />);
    });
    await act(async () => first.resolve({ results: [searchResult("Old vault", "/documents/45", 45, 7)] }));
    await waitFor(() => expect(screen.queryByText("Old vault")).not.toBeInTheDocument());

    fireEvent.change(within(palette).getByRole("textbox", { name: "Search commands" }), {
      target: { value: "beta" },
    });
    await waitFor(() =>
      expect(paletteHarness.unifiedSearch).toHaveBeenCalledWith(expect.objectContaining({ q: "beta" }))
    );
    await act(async () => second.resolve({ results: [searchResult("Current vault", "/documents/46", 46, 8)] }));
    fireEvent.click(await within(await screen.findByRole("dialog")).findByText("Current vault"));
    await waitFor(() => expect(window.location.pathname).toBe("/documents/46"));
  });

  it("drops a late result after principal replacement and navigates the current principal hit", async () => {
    const first = deferred<{ results: ReturnType<typeof searchResult>[] }>();
    const second = deferred<{ results: ReturnType<typeof searchResult>[] }>();
    paletteHarness.unifiedSearch.mockImplementation((request: { q?: string }) => {
      if (request.q === "alpha" && paletteHarness.principalId === 1) return first.promise;
      if (request.q === "alpha") return Promise.resolve({ results: [] });
      if (request.q === "beta") return second.promise;
      return Promise.resolve({ results: [] });
    });
    const { App, view } = await renderAsyncPalette();

    const palette = await typePaletteQuery("alpha");
    await waitFor(() =>
      expect(paletteHarness.unifiedSearch).toHaveBeenCalledWith(expect.objectContaining({ q: "alpha" }))
    );
    await act(async () => {
      paletteHarness.principalId = 2;
      view.rerender(<App />);
    });
    await act(async () => first.resolve({ results: [searchResult("Old principal", "/documents/47", 47)] }));
    await waitFor(() => expect(screen.queryByText("Old principal")).not.toBeInTheDocument());

    fireEvent.change(within(palette).getByRole("textbox", { name: "Search commands" }), {
      target: { value: "beta" },
    });
    await waitFor(() =>
      expect(paletteHarness.unifiedSearch).toHaveBeenCalledWith(expect.objectContaining({ q: "beta" }))
    );
    await act(async () => second.resolve({ results: [searchResult("Current principal", "/documents/48", 48)] }));
    fireEvent.click(await within(await screen.findByRole("dialog")).findByText("Current principal"));
    await waitFor(() => expect(window.location.pathname).toBe("/documents/48"));
  });
});
