import { StrictMode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter } from "react-router-dom";
import LoginPage from "./LoginPage";
import SetupPage from "./SetupPage";

const mocks = vi.hoisted(() => {
  const init = vi.fn();
  return {
    init,
    state: {
      login: vi.fn(),
      register: vi.fn(),
      init,
      needsSetup: null as boolean | null,
      isLoading: false,
      initializationFailed: true,
      authMode: "multi_user",
    },
    captureOwner: vi.fn(),
    capturePrincipalGeneration: vi.fn(),
    isCurrentOwner: vi.fn(),
  };
});

vi.mock("@/stores/useAuthStore", () => {
  const useAuthStore = Object.assign(
    (selector?: (state: typeof mocks.state) => unknown) =>
      selector ? selector(mocks.state) : mocks.state,
    { getState: () => mocks.state },
  );
  return { useAuthStore };
});

vi.mock("@/lib/api/auth-lifecycle", () => ({
  captureAuthOwner: mocks.captureOwner,
  captureAuthPrincipalGeneration: mocks.capturePrincipalGeneration,
  isCurrentAuthOwner: mocks.isCurrentOwner,
}));

const pages = [
  ["Login", LoginPage],
  ["Setup", SetupPage],
] as const;

function renderPublicPage(Page: (typeof pages)[number][1]) {
  return render(
    <StrictMode>
      <BrowserRouter>
        <Page />
      </BrowserRouter>
    </StrictMode>,
  );
}

describe("public authentication initialization retry (F002)", () => {
  beforeEach(() => {
    mocks.init.mockReset();
    mocks.state.login = vi.fn();
    mocks.state.register = vi.fn();
    mocks.state.init = mocks.init;
    mocks.state.needsSetup = null;
    mocks.state.isLoading = false;
    mocks.state.initializationFailed = true;
    mocks.state.authMode = "multi_user";
    mocks.captureOwner.mockReset();
    mocks.capturePrincipalGeneration.mockReset();
    mocks.isCurrentOwner.mockReset();
    mocks.captureOwner.mockReturnValue("retry-owner");
    mocks.capturePrincipalGeneration.mockReturnValue(7);
    mocks.isCurrentOwner.mockReturnValue(true);
  });

  for (const [name, Page] of pages) {
    it(`${name} exposes a fail-closed error and retries through init`, async () => {
      const user = userEvent.setup();
      const view = renderPublicPage(Page);

      expect(screen.getByRole("alert")).toHaveTextContent("Unable to initialize authentication.");
      expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();

      mocks.init.mockClear();
      await user.click(screen.getByRole("button", { name: "Retry" }));
      expect(mocks.init).toHaveBeenCalledTimes(1);

      mocks.state.initializationFailed = false;
      mocks.state.needsSetup = false;
      view.rerender(
        <StrictMode>
          <BrowserRouter>
            <Page />
          </BrowserRouter>
        </StrictMode>,
      );
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it(`${name} rejects a stale retry owner before init`, async () => {
      const user = userEvent.setup();
      mocks.isCurrentOwner.mockReturnValue(false);
      renderPublicPage(Page);

      mocks.init.mockClear();
      await user.click(screen.getByRole("button", { name: "Retry" }));
      expect(mocks.init).not.toHaveBeenCalled();
    });
  }
});
