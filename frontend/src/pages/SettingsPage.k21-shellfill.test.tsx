// Regression checks for issue #494 SettingsPage acceptance (AC22 / UI-029,
// AC23 / UI-030, AC24 / UI-031). These assert REQUIRED behavior that does
// not exist at the pre-fix base (a543361) and are expected to FAIL there;
// each prints an "AC<n> CHECK: FAIL" sentinel immediately before its
// discriminating assertion.
//
// Harness mirrors SettingsPage.capabilitiesInvalidation.test.tsx: the REAL
// SettingsPage + useSettingsStore are exercised with only the @/lib/api
// boundary mocked (getSettings / updateSettings / testConnections),
// useHealthCheck stubbed, and Radix Tabs replaced by jsdom-friendly
// primitives (Radix tabs cannot be pointer-activated under jsdom).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { SettingsResponse, UpdateSettingsRequest } from "@/lib/api";
import { mockSettings } from "@/fixtures/settings";
import { useSettingsStore } from "@/stores/useSettingsStore";

const { mockGetSettings, mockUpdateSettings, mockTestConnections } = vi.hoisted(() => ({
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
// /health. Stub the hook (same pattern as SettingsPage.capabilitiesInvalidation.test.tsx).
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
// primitive to a plain controlled button/div pair — identical approach to
// SettingsPage.capabilitiesInvalidation.test.tsx.
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
    TabsTrigger: ({ value, children }: { value: string; children: React.ReactNode }) => {
      const onValueChange = ReactMod.useContext(ChangeCtx);
      return ReactMod.createElement(
        "button",
        { role: "tab", onClick: () => onValueChange(value) },
        children,
      );
    },
    TabsContent: ({ value, children }: { value: string; children: React.ReactNode }) => {
      const active = ReactMod.useContext(ValueCtx);
      return active === value ? ReactMod.createElement("div", null, children) : null;
    },
  };
});

import SettingsPage from "@/pages/SettingsPage";

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

describe("SettingsPage issue #494 acceptance checks", () => {
  beforeEach(() => {
    // Zustand store is a module-level singleton — reset between tests.
    useSettingsStore.getState().resetState();
    mockGetSettings.mockReset().mockResolvedValue(mockSettings);
    mockTestConnections.mockReset().mockResolvedValue({});
    mockUpdateSettings.mockReset().mockImplementation(
      async (payload: UpdateSettingsRequest): Promise<SettingsResponse> => ({
        ...mockSettings,
        ...payload,
      }),
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("K-21: a failed page load keeps the error card when the SHELL already filled the store", async () => {
    // Simulate PageShell's shell fill: the store holds a (possibly admin)
    // payload BEFORE the Settings page mounts. The page's own GET /settings
    // then fails. The error card (with Retry) must persist — the shell-filled
    // values must NOT be presented as the editable current configuration.
    useSettingsStore.getState().setSettings({
      ...mockSettings,
      retrieval_top_k: 17,
    } as never);

    mockGetSettings.mockReset().mockRejectedValue(new Error("shell down"));
    renderSettingsPage();

    await waitFor(() => {
      expect(screen.getByText(/Error:/)).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("K-21 reverse order: a late shell fill cannot flip the error card into the defaults form", async () => {
    // The page's own fetch fails FIRST (error card renders); PageShell's
    // shell response lands AFTER — the error card must survive it.
    mockGetSettings.mockReset().mockRejectedValue(new Error("page fetch down"));
    renderSettingsPage();

    await waitFor(() => {
      expect(screen.getByText(/Error:/)).toBeInTheDocument();
    });

    // Late shell fill: the store's settings is still null (the failed page
    // load never wrote one), so the null-only guard passes and the shell
    // payload lands — the error card must STILL win over the form.
    act(() => {
      useSettingsStore.getState().setSettings(mockSettings as never);
    });

    expect(screen.getByText(/Error:/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save changes/i })).toBeNull();
  });
});
