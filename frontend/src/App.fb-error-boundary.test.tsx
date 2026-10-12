import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

// Black-box shell coverage for #779. The page changes from healthy route A to
// healthy route B, then the B page begins throwing without a location-token
// change. Separate query-only and hash-only recoveries prove the complete
// PageShell token, while the stateful chat mock proves the /chat/821 parameter.
const authState = vi.hoisted(() => ({
  init: vi.fn().mockResolvedValue(undefined),
  user: undefined,
  isAuthenticated: true,
  isInitialized: true,
  isLoading: false,
  needsSetup: false,
}));

if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

vi.mock("@/stores/useAuthStore", () => {
  type AuthStoreMock = ((selector: (state: Record<string, unknown>) => unknown) => unknown) & {
    getState: () => typeof authState;
    setState: (partial: Partial<typeof authState>) => void;
  };
  const useAuthStore = ((selector: (state: Record<string, unknown>) => unknown) =>
    selector(authState)) as AuthStoreMock;
  useAuthStore.getState = () => authState;
  useAuthStore.setState = (partial) => Object.assign(authState, partial);
  return { useAuthStore };
});
vi.mock("@/components/auth/ProtectedRoute", () => ({
  ProtectedRoute: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/hooks/useHealthCheck", () => ({
  useHealthCheck: () => ({ backend: true, embeddings: true, chat: true, loading: false }),
}));
vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomCapabilities: () => ({ data: undefined, isLoading: false, isError: false }),
  useDraftRoomVisible: () => false,
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getSettings: async () => ({ chat_configured: true }),
}));

vi.mock("@/pages/VaultsPage", () => ({ default: () => <div>Healthy route A</div> }));
vi.mock("@/pages/DocumentsPage", async () => {
  const { useLocation } = await import("react-router-dom");
  const { useState } = await import("react");
  return {
    default: function DocumentsFeedbackPage() {
      const location = useLocation();
      const [crashing, setCrashing] = useState(false);
      if (crashing) {
        throw new Error(`feedback boundary ${location.pathname}${location.search}${location.hash}`);
      }
      const label = location.search.includes("query") || location.search.includes("recovered")
        ? location.hash.includes("second")
          ? "Healthy hash recovery"
          : "Healthy query recovery"
        : "Healthy route B";
      return (
        <>
          <div>{label}</div>
          <button type="button" onClick={() => setCrashing(true)}>
            Crash route B
          </button>
        </>
      );
    },
  };
});
vi.mock("@/pages/ChatShell", async () => {
  const { useState } = await import("react");
  const { useLocation, useParams } = await import("react-router-dom");
  const { useIsPresent } = await import("framer-motion");
  return {
    default: function StatefulChatMock() {
      const location = useLocation();
      const { sessionId } = useParams();
      const isPresent = useIsPresent();
      const [draft, setDraft] = useState("");
      const routeKey = `${location.pathname}${location.search}${location.hash}`;
      return (
        <div
          data-testid="chat-session"
          data-presence={isPresent ? "present" : "exiting"}
          data-route-key={routeKey}
        >
          <div>Chat session {sessionId ?? "new"}</div>
          <label>
            Chat draft
            <input
              aria-label="Chat draft"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
        </div>
      );
    },
  };
});
vi.mock("@/pages/DocumentDetailPage", () => ({ default: () => <div>Document Detail Page</div> }));
vi.mock("@/pages/MemoryPage", () => ({ default: () => <div>Memory Page</div> }));
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
vi.mock("@/pages/SearchPage", () => ({ default: () => <div>Search Page</div> }));
vi.mock("@/pages/DraftRoomPage", () => ({ default: () => <div>Draft Room Page</div> }));
vi.mock("@/pages/DraftRoomDetailPage", () => ({ default: () => <div>Draft Room Detail Page</div> }));
vi.mock("@/components/canvas/CanvasPage", () => ({ default: () => <div>Canvas Page</div> }));

async function navigateTo(path: string) {
  await act(async () => {
    window.history.pushState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}
async function findPresentChatSession(routeKey: string): Promise<HTMLElement> {
  let settled: HTMLElement | undefined;
  await waitFor(() => {
    const sessions = screen.getAllByTestId("chat-session");
    expect(sessions).toHaveLength(1);
    const soleSession = sessions[0];
    if (!soleSession) throw new Error("Missing sole chat session");
    expect(soleSession.dataset.presence).toBe("present");
    expect(soleSession.dataset.routeKey).toBe(routeKey);
    settled = soleSession;
  });
  if (!settled) throw new Error("No settled chat session");
  return settled;
}

describe("App shell feedback recovery (#779)", { timeout: 30_000 }, () => {
  afterEach(() => {
    authState.user = undefined;
    authState.isAuthenticated = true;
    vi.restoreAllMocks();
  });

  it("keeps navigation through healthy A to healthy B, crash B, and chat 821 recovery", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    await act(async () => {
      window.history.pushState({}, "", "/vaults");
    });
    const { default: App } = await import("./App");
    render(<App />);

    expect(await screen.findByText("Healthy route A")).toBeInTheDocument();
    await navigateTo("/documents");
    expect(await screen.findByText("Healthy route B")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Crash route B" }));
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getAllByRole("navigation").length).toBeGreaterThan(0);

    await navigateTo("/chat");
    expect(await screen.findByText("Chat session new")).toBeInTheDocument();
    await navigateTo("/chat/821");
    expect(await screen.findByText("Chat session 821")).toBeInTheDocument();
    const settledChat = await findPresentChatSession("/chat/821");
    const draft = within(settledChat).getByRole("textbox", { name: "Chat draft" });
    fireEvent.change(draft, { target: { value: "draft survives same pathname" } });
    await navigateTo("/chat/821?view=next#first");
    expect(await screen.findByText("Chat session 821")).toBeInTheDocument();
    const firstSettledChat = await findPresentChatSession("/chat/821?view=next#first");
    expect(within(firstSettledChat).getByRole("textbox", { name: "Chat draft" })).toHaveValue(
      "draft survives same pathname",
    );
    await navigateTo("/chat/821?view=next#second");
    const secondSettledChat = await findPresentChatSession("/chat/821?view=next#second");
    expect(within(secondSettledChat).getByRole("textbox", { name: "Chat draft" })).toHaveValue(
      "draft survives same pathname",
    );
    expect(screen.getAllByRole("navigation").length).toBeGreaterThan(0);
    expect(errorSpy.mock.calls.some((call) =>
      call.some(
        (argument, index) =>
          index === 0 &&
          typeof argument === "string" &&
          argument.includes("Cannot update a component") &&
          argument.includes("while rendering a different component"),
      ),
    )).toBe(false);
  });

  it("recovers a crashing shell on query-only and hash-only changes with the same pathname", async () => {
    await act(async () => {
      window.history.pushState({}, "", "/documents?mode=query#first");
    });
    const { default: App } = await import("./App");
    render(<App />);
    expect(await screen.findByText("Healthy query recovery")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Crash route B" }));
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    await navigateTo("/documents?mode=recovered#first");
    expect(await screen.findByText("Healthy query recovery")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Crash route B" }));
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    await navigateTo("/documents?mode=recovered#second");
    expect(await screen.findByText("Healthy hash recovery")).toBeInTheDocument();
  });
});
