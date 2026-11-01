// Issue #775 AC6: the empty palette must expose real, non-navigation actions
// in the /chat context. The real App is mounted so MainAppShell, PageShell,
// Navigation, CommandPalette, ProtectedRoute, and ChatShell share the same
// provider topology as production. Each action is observed through the owner
// it already serves: chat store, VaultSelector, ChatShell's shortcut dialog,
// theme store, and Composer's react-dropzone seam. No action implementation is
// mocked; only unrelated routes and fixture/network boundaries are isolated.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within, cleanup } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SHORTCUT_BINDINGS_STORAGE_KEY } from "@/lib/shortcutBindings";
import { useChatStore } from "@/stores/useChatStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { useThemeStore } from "@/stores/useThemeStore";

const actionHarness = vi.hoisted(() => ({
  dropzoneOpen: vi.fn(),
}));

// Keep react-dropzone's real hook and DOM props; only expose its existing open
// seam so this check can observe the attach action without opening a file picker.
vi.mock("react-dropzone", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-dropzone")>();
  return {
    ...actual,
    useDropzone: (options: Parameters<typeof actual.useDropzone>[0]) => {
      const state = actual.useDropzone(options);
      return { ...state, open: actionHarness.dropzoneOpen };
    },
  };
});

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getSettings: vi.fn().mockResolvedValue({ chat_configured: true }),
  };
});

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

vi.mock("@/lib/api/search", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/search")>();
  return { ...actual, unifiedSearch: vi.fn().mockResolvedValue({ results: [] }) };
});

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      user: { id: 1, username: "alice", role: "admin" },
      logout: vi.fn(),
      init: vi.fn().mockResolvedValue(undefined),
    }),
}));

vi.mock("@/components/auth/ProtectedRoute", () => ({
  ProtectedRoute: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("@/hooks/useHealthCheck", () => ({
  useHealthCheck: () => ({ backend: true, embeddings: true, chat: true, loading: false }),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => true,
}));

// Keep the App, shell, router, and real /chat page mounted while isolating
// unrelated lazy route modules from their network and page-level effects.
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

const shortcutStorage = new Map<string, string>();

async function openPalette() {
  fireEvent.keyDown(document.body, { key: "k", code: "KeyK", ctrlKey: true });
  let palette: HTMLElement;
  await vi.waitFor(() => {
    palette = screen.getByRole("dialog");
  });
  return palette!;
}

async function renderChatActionOwners() {
  // App reads TEST_MODE at module evaluation, so the env must be set before
  // this dynamic import. TestModeProvider then remains the real provider in
  // MainAppShell; its fixture branch does not replace owner handlers.
  vi.stubEnv("VITE_TEST_MODE", "true");
  window.history.pushState({}, "", "/chat");
  // App keeps the /chat route behind React.lazy. Resolve the real page module
  // before mounting App so RTL never observes the production Suspense loader
  // as if it were the action-owner fixture. The route and provider hierarchy
  // remain the real App after this preload.
  await act(async () => {
    await import("@/pages/ChatShell");
  });
  const { default: App } = await import("../App");
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  );
  // Wait for the real lazy ChatShell route before sending Ctrl+K. This also
  // lets its mount effects settle before the test seeds observable state.
  await screen.findByRole("heading", { name: "Chat" }, { timeout: 3000 });
}

beforeAll(async () => {
  vi.stubEnv("VITE_TEST_MODE", "true");
  await act(async () => {
    await import("@/pages/ChatShell");
    await import("../App");
  });
}, 10_000);
describe("#775 chat palette actions", () => {
  function resetOwnerState() {
    actionHarness.dropzoneOpen.mockClear();
    useChatStore.getState().loadChat("seed", [
      { id: "seed-message", role: "user", content: "seed message" },
    ]);
    useVaultStore.setState({
      activeVaultId: null,
      vaults: [
        { id: 7, name: "Research", file_count: 0 },
        { id: 8, name: "Archive", file_count: 0 },
      ],
    });
    useThemeStore.getState().setTheme("light");
  }

  beforeEach(() => {
    vi.stubEnv("VITE_TEST_MODE", "true");
    shortcutStorage.clear();
    vi.mocked(window.localStorage.getItem).mockImplementation(
      (key: string) => shortcutStorage.get(key) ?? null
    );
    vi.mocked(window.localStorage.setItem).mockImplementation(
      (key: string, value: string) => {
        shortcutStorage.set(key, value);
      }
    );
    vi.mocked(window.localStorage.removeItem).mockImplementation((key: string) => {
      shortcutStorage.delete(key);
    });
    vi.mocked(window.localStorage.clear).mockImplementation(() => {
      shortcutStorage.clear();
    });
    // AC1 reserves Ctrl+K for this palette in the /chat harness; the real
    // session-search owner keeps its advertised behavior on a different key.
    window.localStorage.setItem(
      SHORTCUT_BINDINGS_STORAGE_KEY,
      JSON.stringify({ focusSearch: "F8" })
    );
    resetOwnerState();
  });

  afterEach(() => {
    cleanup();
    useChatStore.getState().newChat();
    useVaultStore.setState({ activeVaultId: null, vaults: [] });
    vi.unstubAllEnvs();
  });

  it("palette lists at least five actions and executes each without changing /chat", async () => {
    const actions = [
      "New chat",
      "Switch vault",
      "Open keyboard shortcuts",
      "Cycle theme",
      "Attach file",
    ];

    for (const label of actions) {
      // Retire the previous iteration's owner state before mounting the next
      // App. Seed again after the real route has settled, before opening the
      // palette, so opening never observes a context that is changed later.
      resetOwnerState();
      await renderChatActionOwners();
      resetOwnerState();
      if (label === actions[0]) {
        expect(useChatStore.getState().activeChatId).toBe("seed");
      }
      const palette = await openPalette();
      if (label === actions[0]) {
        const actionButtons = within(palette).getAllByRole("button", {
          name: /^(New chat|Switch vault|Open keyboard shortcuts|Cycle theme|Attach file)$/,
        });
        expect(actionButtons.length).toBeGreaterThanOrEqual(actions.length);
      }
      const beforePath = window.location.pathname;
      expect(beforePath).toBe("/chat");
      expect(useChatStore.getState().activeChatId).toBe("seed");
      expect(useChatStore.getState().messageIds).toHaveLength(1);
      expect(useVaultStore.getState().activeVaultId).toBeNull();
      expect(useVaultStore.getState().vaults).toHaveLength(2);
      expect(useThemeStore.getState().theme).toBe("light");
      const action = within(palette).getByRole("button", { name: label });
      fireEvent.click(action);

      switch (label) {
        case "New chat":
          await vi.waitFor(() => expect(useChatStore.getState().activeChatId).toBeNull());
          break;
        case "Switch vault":
          await vi.waitFor(() => {
            expect(screen.getByRole("menuitem", { name: /Research/ })).toBeInTheDocument();
          });
          fireEvent.click(screen.getByRole("menuitem", { name: /Research/ }));
          await vi.waitFor(() => {
            expect(useVaultStore.getState().activeVaultId).toBe(7);
            expect(
              screen.getByRole("button", { name: /Active vault: Research/i })
            ).toBeInTheDocument();
          });
          break;
        case "Open keyboard shortcuts":
          await vi.waitFor(() => {
            expect(screen.getByRole("dialog", { name: /Keyboard Shortcuts/i })).toBeInTheDocument();
          });
          break;
        case "Cycle theme":
          await vi.waitFor(() => expect(useThemeStore.getState().theme).not.toBe("light"));
          break;
        case "Attach file":
          await vi.waitFor(() => expect(actionHarness.dropzoneOpen).toHaveBeenCalledTimes(1));
          break;
      }
      expect(window.location.pathname).toBe(beforePath);
      cleanup();
    }
  }, 15_000);
});
