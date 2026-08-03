/**
 * Integration coverage for the save -> draft-room-capabilities-cache
 * invalidation seam in SettingsPage.
 *
 * draftRoomCapabilitiesInvalidation.test.ts only unit-tests the trivial
 * predicate (payloadTogglesDraftRoom). It never proves the predicate is
 * actually wired into SettingsPage's real save flow — a refactor could
 * silently drop the `queryClient.invalidateQueries(...)` call (or call it
 * unconditionally, wastefully invalidating on every save) and that unit
 * test would keep passing. This file renders the real SettingsPage under a
 * QueryClientProvider, drives an actual save, and asserts the query-client
 * side effect directly.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { draftRoomKeys } from "@/lib/api/draftRoom";
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

// SettingsPage mounts useHealthCheck, which hits the real apiClient/GET
// /health. Stub the hook (same pattern as App.draft-room.test.tsx) so this
// file only exercises the settings save flow, not network health polling.
vi.mock("@/hooks/useHealthCheck", () => ({
  useHealthCheck: () => ({
    backend: true,
    embeddings: true,
    chat: true,
    loading: false,
    lastChecked: null,
  }),
}));

// Radix Tabs cannot be activated via fireEvent.click in jsdom (pointer-capture
// / activation semantics never fire). Mock the primitive to a plain
// controlled button/div pair — identical approach to WikiPage.test.tsx line
// ~49, the established pattern in this repo (see
// .claude/skills/ci-compatibility-audit/references/frontend-testing-gotchas.md).
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
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  render(
    <QueryClientProvider client={queryClient}>
      <SettingsPage />
    </QueryClientProvider>,
  );
  return { invalidateSpy };
}

describe("SettingsPage save -> draft room capabilities cache invalidation", () => {
  beforeEach(() => {
    // Zustand store is a module-level singleton — reset between tests so a
    // dirty field from one test doesn't leak into the next.
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

  it("invalidates draftRoomKeys.capabilities() after saving a dirty draft_room_enabled toggle", async () => {
    const { invalidateSpy } = renderSettingsPage();

    // Wait for the real getSettings() load to resolve and the tab bar to
    // mount (loading skeleton -> real content).
    fireEvent.click(await screen.findByRole("tab", { name: /maintenance/i }));

    const checkbox = await screen.findByRole("checkbox", { name: "Enable Draft Room" });
    expect(checkbox).toHaveAttribute("aria-checked", "false");
    fireEvent.click(checkbox);
    expect(checkbox).toHaveAttribute("aria-checked", "true");

    const saveButton = await screen.findByRole("button", { name: "Save Changes" });
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);

    await waitFor(() => expect(mockUpdateSettings).toHaveBeenCalledTimes(1));
    // Only the dirty field should be in the payload — proves this is really
    // the toggle-driven save, not some other dirty field.
    expect(mockUpdateSettings).toHaveBeenCalledWith({ draft_room_enabled: true });

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: draftRoomKeys.capabilities(),
      }),
    );
  });

  it("does NOT invalidate draftRoomKeys.capabilities() when saving an unrelated dirty field", async () => {
    const { invalidateSpy } = renderSettingsPage();

    // Confirm the initial load completed before mutating the store directly.
    await screen.findByRole("tab", { name: /overview/i });

    // retrieval_top_k is unrelated to draft_room_enabled and is NOT in
    // REINDEX_REQUIRED_FIELDS, so this save takes the direct persistSave()
    // path without the reindex-confirmation dialog getting in the way. Per
    // the task's fallback guidance, driving the store directly here targets
    // the save->invalidate seam rather than re-testing checkbox wiring
    // (already covered by DraftRoomSettings.test.tsx).
    const nextTopK = mockSettings.retrieval_top_k + 1;
    act(() => {
      useSettingsStore.getState().updateFormField("retrieval_top_k", nextTopK);
    });

    const saveButton = await screen.findByRole("button", { name: "Save Changes" });
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);

    await waitFor(() => expect(mockUpdateSettings).toHaveBeenCalledTimes(1));
    expect(mockUpdateSettings).toHaveBeenCalledWith({ retrieval_top_k: nextTopK });

    // Give any (incorrect) invalidation a chance to fire before asserting
    // its absence.
    await waitFor(() => expect(useSettingsStore.getState().saving).toBe(false));
    expect(invalidateSpy).not.toHaveBeenCalledWith({
      queryKey: draftRoomKeys.capabilities(),
    });
  });
});
