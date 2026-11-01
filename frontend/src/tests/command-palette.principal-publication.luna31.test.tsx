import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CommandPalette } from "@/components/shared/CommandPalette";
import {
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";

const paletteHarness = vi.hoisted(() => ({
  principalId: 1,
  role: "admin",
  unifiedSearch: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: (selector: (state: { user: { id: number; role: string } }) => unknown) =>
    selector({
      user: {
        id: paletteHarness.principalId,
        role: paletteHarness.role,
      },
    }),
}));

vi.mock("@/hooks/useDraftRoomCapabilities", () => ({
  useDraftRoomVisible: () => true,
}));

vi.mock("@/lib/api/search", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/search")>();
  return { ...actual, unifiedSearch: paletteHarness.unifiedSearch };
});

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: (selector: (state: { activeVaultId: number | null }) => unknown) =>
    selector({ activeVaultId: null }),
}));

const themeState = {
  theme: "light" as const,
  setTheme: vi.fn(),
};

vi.mock("@/stores/useThemeStore", () => ({
  useThemeStore: (selector: (state: typeof themeState) => unknown) => selector(themeState),
}));

vi.mock("@/stores/useNavigationGuardStore", () => ({
  useNavigationGuardStore: {
    getState: () => ({ confirmLeave: null }),
  },
}));

vi.mock("@/lib/commandPaletteActions", () => {
  const actions: never[] = [];
  return {
    getCommandPaletteActionSnapshot: () => actions,
    subscribeCommandPaletteActions: () => () => undefined,
    invokeCommandPaletteAction: () => false,
  };
});

vi.mock("@/components/shared/KeyboardShortcuts", () => ({
  bindingFor: () => "?",
}));

vi.mock("sonner", () => ({ toast }));

function LocationProbe() {
  const { pathname } = useLocation();
  return <output data-testid="pathname">{pathname}</output>;
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function searchResult(title: string, urlHint: string, id: number) {
  return {
    type: "document" as const,
    id,
    title,
    snippet: `${title} snippet`,
    vault_id: 1,
    url_hint: urlHint,
    score: 1,
  };
}

function openPalette() {
  fireEvent.keyDown(document.body, { key: "k", code: "KeyK", ctrlKey: true, cancelable: true });
  return screen.getByRole("dialog");
}

function renderPalette(initialEntry = "/vaults") {
  window.history.pushState({}, "", initialEntry);
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <LocationProbe />
      <CommandPalette />
    </MemoryRouter>
  );
}

let previousClipboardDescriptor: PropertyDescriptor | undefined;
let previousHref = "";

beforeEach(() => {
  paletteHarness.principalId = 1;
  paletteHarness.role = "admin";
  paletteHarness.unifiedSearch.mockReset();
  themeState.setTheme.mockReset();
  toast.success.mockReset();
  toast.error.mockReset();
  publishAuthPrincipal({ id: 1, role: "admin" });
  previousClipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  previousHref = window.location.href;
});

afterEach(() => {
  cleanup();
  publishAuthPrincipal(null);
  if (previousClipboardDescriptor) {
    Object.defineProperty(navigator, "clipboard", previousClipboardDescriptor);
  } else {
    Reflect.deleteProperty(navigator, "clipboard");
  }
  window.history.replaceState({}, "", previousHref);
});

describe("#775 CommandPalette principal publication", () => {
  it("does not publish or navigate a held search result after a real principal-generation ABA", async () => {
    const held = deferred<{ results: ReturnType<typeof searchResult>[] }>();
    paletteHarness.unifiedSearch.mockReturnValue(held.promise);

    renderPalette();
    const dialog = openPalette();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Search commands" }), {
      target: { value: "alpha" },
    });
    await waitFor(() =>
      expect(paletteHarness.unifiedSearch).toHaveBeenCalledWith(
        expect.objectContaining({ q: "alpha" })
      )
    );

    await act(async () => {
      // The visible principal returns to the same id/role, so only the real
      // lifecycle generation distinguishes this context from the stale one.
      publishAuthPrincipal(null);
      publishAuthPrincipal({ id: 1, role: "admin" });
      held.resolve({ results: [searchResult("ABA stale result", "/documents/91", 91)] });
    });

    await waitFor(() => expect(screen.queryByText("ABA stale result")).not.toBeInTheDocument());
    expect(screen.getByTestId("pathname")).toHaveTextContent("/vaults");
  });

  it("does not toast a copy result after principal replacement while the owner stays current", async () => {
    const copied = deferred<void>();
    const writeText = vi.fn().mockReturnValue(copied.promise);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    renderPalette("/documents/7");
    fireEvent.click(within(openPalette()).getByRole("button", { name: "Copy page link" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringContaining("/documents/7")));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await act(async () => {
      paletteHarness.principalId = 2;
      paletteHarness.role = "member";
      publishAuthPrincipal({ id: 2, role: "member" });
      copied.resolve(undefined);
    });

    await act(async () => undefined);
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("does not toast a copy result after owner replacement with the same principal", async () => {
    const copied = deferred<void>();
    const writeText = vi.fn().mockReturnValue(copied.promise);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    renderPalette("/documents/7");
    fireEvent.click(within(openPalette()).getByRole("button", { name: "Copy page link" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringContaining("/documents/7")));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await act(async () => {
      reserveReplacementAuthOwner();
      copied.resolve(undefined);
    });

    await act(async () => undefined);
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("does not toast a copy result after the palette unmounts", async () => {
    const copied = deferred<void>();
    const writeText = vi.fn().mockReturnValue(copied.promise);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    const view = renderPalette("/documents/8");
    fireEvent.click(within(openPalette()).getByRole("button", { name: "Copy page link" }));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    view.unmount();

    await act(async () => copied.resolve(undefined));
    await act(async () => undefined);
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("reports a same-auth copy after the dialog closes", async () => {
    const copied = deferred<void>();
    const writeText = vi.fn().mockReturnValue(copied.promise);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    renderPalette("/documents/9");
    fireEvent.click(within(openPalette()).getByRole("button", { name: "Copy page link" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringContaining("/documents/9")));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await act(async () => copied.resolve(undefined));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Page link copied"));
    expect(toast.error).not.toHaveBeenCalled();
  });
});
