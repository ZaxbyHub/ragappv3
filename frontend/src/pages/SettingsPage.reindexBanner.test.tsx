// Issue #736/#833 final-critic round-2 pin: the reindex-required banner's
// 503/recovery copy is driven by `reindexIdentityChanged`, which must stay
// sticky once set — a LATER non-identity save in the same session must not
// switch the banner back to the no-503 copy while the backend gate is still
// down (only an all-vault reindex lifts it).
//
// Harness mirrors SettingsPage.k21-shellfill.test.tsx: the REAL SettingsPage
// + useSettingsStore with only the @/lib/api boundary mocked and Radix Tabs
// replaced by jsdom-friendly primitives.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { mockSettings } from "@/fixtures/settings";
import { useSettingsStore } from "@/stores/useSettingsStore";
import type { SettingsResponse, UpdateSettingsRequest } from "@/lib/api";

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

vi.mock("@/hooks/useHealthCheck", () => ({
  useHealthCheck: () => ({
    backend: true,
    embeddings: true,
    chat: true,
    loading: false,
    lastChecked: null,
  }),
}));

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

import SettingsPage from "./SettingsPage";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <SettingsPage />
    </QueryClientProvider>,
  );
}

/** Drive one real save: set a field dirty, then click through the confirm
 * dialog if it appears (identity fields open ReindexConfirmDialog). */
async function saveWithField(field: "embedding_model" | "chunk_size_chars") {
  const store = useSettingsStore.getState();
  const current = store.formData;
  const next =
    field === "embedding_model"
      ? { ...current, embedding_model: current.embedding_model + "-x" }
      : { ...current, chunk_size_chars: current.chunk_size_chars + 7 };
  act(() => {
    useSettingsStore.getState().setFormData(next);
  });
  const saveButton = await screen.findByRole("button", {
    name: /save changes/i,
  });
  await act(async () => {
    saveButton.dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true }),
    );
  });
  // The identity field opens the confirm dialog — click it through.
  await waitFor(
    () => {
      const dialog = screen.queryByRole("dialog");
      if (!dialog) return;
      const confirm = Array.from(
        dialog.querySelectorAll("button"),
      ).find((b) => /save and acknowledge/i.test(b.textContent ?? ""));
      if (confirm && !confirm.hasAttribute("disabled")) {
        confirm.click();
      }
    },
    { timeout: 3000 },
  );
  await waitFor(
    () => {
      expect(useSettingsStore.getState().saving).toBe(false);
    },
    { timeout: 5000 },
  );
}

describe("SettingsPage reindex banner stickiness (issue #736/#833)", () => {
  beforeEach(() => {
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
  afterEach(() => cleanup());

  it("keeps the 503 copy after a later chunk-only save (final critic round 2)", async () => {
    renderPage();
    await waitFor(() => {
      expect(useSettingsStore.getState().settings).not.toBeNull();
    });

    // Save 1: identity field -> flag true, 503 banner copy.
    await saveWithField("embedding_model");
    const store1 = useSettingsStore.getState();
    expect(store1.reindexRequired).toBe(true);
    expect(store1.reindexIdentityChanged).toBe(true);
    expect(screen.getByText(/embedding-mismatch 503/i)).toBeInTheDocument();

    // Save 2: chunk-only — the gate is STILL down backend-side; the banner
    // must keep the 503 copy (sticky), not switch to the no-503 wording.
    await saveWithField("chunk_size_chars");
    expect(useSettingsStore.getState().reindexIdentityChanged).toBe(true);
    expect(screen.getByText(/embedding-mismatch 503/i)).toBeInTheDocument();

    // Dismiss clears it and the banner unmounts.
    act(() => {
      useSettingsStore.getState().setReindexRequired(false);
      useSettingsStore.getState().setReindexIdentityChanged(false);
    });
    expect(screen.queryByText(/embedding-mismatch 503/i)).not.toBeInTheDocument();
  }, 20000);
});
