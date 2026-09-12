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

  it("AC22: a rejected initial getSettings clears loading, shows an actionable error, and offers a retry that recovers", async () => {
    mockGetSettings.mockReset().mockRejectedValue(
      new Error("Settings service unreachable"),
    );
    renderSettingsPage();

    console.log("AC22 CHECK: FAIL");

    // (b) An actionable error is visible. At base the rejection handler
    // sets `error` but never clears `loading` (store default true; only
    // initializeForm clears it), so the skeleton render short-circuits and
    // this text never appears.
    expect(
      await screen.findByText(
        /settings service unreachable/i,
        {},
        { timeout: 1500 },
      ),
    ).toBeInTheDocument();

    // (a) The loading skeleton is NOT perpetually rendered — the loading
    // state was cleared, and no skeleton (role="status") remains.
    expect(useSettingsStore.getState().loading).toBe(false);
    expect(screen.queryAllByRole("status")).toHaveLength(0);

    // (c) A retry affordance exists, and a successful retry (API healed)
    // displays the settings content.
    const retryButton = await screen.findByRole("button", {
      name: /retry|try again|reload/i,
    });
    mockGetSettings.mockResolvedValueOnce(mockSettings);
    fireEvent.click(retryButton);

    expect(await screen.findByRole("tab", { name: /overview/i })).toBeInTheDocument();
    expect(useSettingsStore.getState().loading).toBe(false);
    expect(useSettingsStore.getState().settings).not.toBeNull();
  });

  it("AC23: correcting the invalid field after a failed save re-enables Save while the unrelated edit stays dirty", async () => {
    renderSettingsPage();
    // Wait for the initial load to complete (tab bar renders post-load).
    await screen.findByRole("tab", { name: /overview/i });

    // One invalid field (retrieval_window must be 0..3) plus one unrelated
    // VALID dirty edit (snapshot value is mockSettings.retrieval_top_k = 8).
    act(() => {
      useSettingsStore.getState().updateFormField("retrieval_window", 9);
      useSettingsStore.getState().updateFormField("retrieval_top_k", 7);
    });

    const saveButton = screen.getByRole("button", { name: "Save Changes" });
    expect(saveButton).toBeEnabled();

    // Run the real handleSave: validateForm() fails, errors are set, and
    // the footer disables Save (SettingsPage validationFailed + footer
    // `invalid` guard).
    fireEvent.click(saveButton);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save Changes" })).toBeDisabled(),
    );
    expect(useSettingsStore.getState().errors.retrieval_window).toBeTruthy();

    console.log("AC23 CHECK: FAIL");

    // Correct the invalid field. updateFormField must clear/recompute the
    // stale validation errors so Save becomes enabled again...
    act(() => {
      useSettingsStore.getState().updateFormField("retrieval_window", 2);
    });

    expect(screen.getByRole("button", { name: "Save Changes" })).toBeEnabled();

    // ...while the unrelated edit is still pending — Save enabled must come
    // from validation recovering, not from the form going clean.
    const dirty = useSettingsStore.getState().dirtyFields();
    expect(dirty.has("retrieval_top_k")).toBe(true);
    expect(dirty.has("retrieval_window")).toBe(true);
  });

  it("AC24: an edit made while a save is in flight is preserved when the save resolves with the old snapshot", async () => {
    renderSettingsPage();
    await screen.findByRole("tab", { name: /overview/i });
    // The retrieval tab hosts the numeric inputs used to observe editing.
    fireEvent.click(screen.getByRole("tab", { name: /retrieval/i }));

    const snapshotTopK = mockSettings.retrieval_top_k; // 8
    const firstEdit = snapshotTopK + 1; // 9 — travels in the save payload
    const newerEdit = firstEdit + 5; // 14 — made while the save is in flight

    act(() => {
      useSettingsStore.getState().updateFormField("retrieval_top_k", firstEdit);
    });

    // Hold the PUT /settings response so the save stays in flight.
    let resolveSave!: (value: SettingsResponse) => void;
    const pendingSave = new Promise<SettingsResponse>((resolve) => {
      resolveSave = resolve;
    });
    mockUpdateSettings.mockReturnValueOnce(pendingSave);

    fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));
    await waitFor(() => expect(useSettingsStore.getState().saving).toBe(true));
    // The in-flight save carries the pre-race value.
    await waitFor(() =>
      expect(mockUpdateSettings).toHaveBeenCalledWith({ retrieval_top_k: firstEdit }),
    );

    // While pending: is editing visibly disabled (the alternative contract)?
    const retrievalWindowInput = screen.getByLabelText(
      "Retrieval Window",
    ) as HTMLInputElement;
    const editingDisabledDuringSave =
      retrievalWindowInput.disabled ||
      retrievalWindowInput.hasAttribute("aria-disabled");

    // The user makes a NEWER edit while the request is in flight.
    act(() => {
      useSettingsStore.getState().updateFormField("retrieval_top_k", newerEdit);
    });

    // The server acknowledges the OLD snapshot (the payload it received).
    resolveSave({ ...mockSettings, retrieval_top_k: firstEdit });
    await waitFor(() => expect(useSettingsStore.getState().saving).toBe(false));

    console.log("AC24 CHECK: FAIL");

    // Contract: the newer edit must not be silently lost — either it is
    // still present in formData AND still dirty (preferred), or editing was
    // visibly disabled during the save so the race could not occur. At base
    // initializeForm(old snapshot) overwrites both formData and
    // loadedFormData: the newer value is clobbered and dirtyCount drops to 0.
    const state = useSettingsStore.getState();
    const newerEditPreserved =
      state.formData.retrieval_top_k === newerEdit &&
      state.dirtyFields().has("retrieval_top_k");
    expect(newerEditPreserved || editingDisabledDuringSave).toBe(true);
  });
});
