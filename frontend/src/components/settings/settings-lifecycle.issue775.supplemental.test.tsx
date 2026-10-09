import { useState, type ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModelsTab } from "@/components/settings/ModelsTab";
import { WikiCuratorSettings } from "@/components/settings/WikiCuratorSettings";
import { useAuthStore } from "@/stores/useAuthStore";
import { useSettingsStore, type SettingsFormData } from "@/stores/useSettingsStore";
import { reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";

const api = vi.hoisted(() => ({
  getVault: vi.fn(),
  toggleVaultMultimodalProvider: vi.fn(),
  testCuratorConnection: vi.fn(),
  toggleVaultEnrichment: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, ...api };
});

type ModelsProps = ComponentProps<typeof ModelsTab>;

const userA = {
  id: 101,
  username: "owner-a",
  full_name: "Owner A",
  role: "member" as const,
  is_active: true,
};
const userB = {
  id: 202,
  username: "owner-b",
  full_name: "Owner B",
  role: "member" as const,
  is_active: true,
};

let authSnapshot: ReturnType<typeof useAuthStore.getState>;
const pendingSettlements = new Set<() => void>();
type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
};

function setAuth(user: typeof userA, token: string) {
  act(() => {
    useAuthStore.setState({
      user,
      accessToken: token,
      isAuthenticated: true,
      isLoading: false,
      isInitialized: true,
    });
  });
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  let settled = false;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = (value) => {
      if (settled) return;
      settled = true;
      pendingSettlements.delete(cleanupSettlement);
      nextResolve(value);
    };
    reject = (reason) => {
      if (settled) return;
      settled = true;
      pendingSettlements.delete(cleanupSettlement);
      nextReject(reason);
    };
  });
  const cleanupSettlement = () => resolve(undefined as T);
  pendingSettlements.add(cleanupSettlement);
  return { promise, resolve, reject };
}

async function resolveDeferred<T>(held: Deferred<T>, value: T) {
  await act(async () => {
    held.resolve(value);
    await Promise.resolve();
  });
}

function baseFormData(overrides: Partial<SettingsFormData> = {}): SettingsFormData {
  return { ...useSettingsStore.getState().formData, ...overrides } as SettingsFormData;
}

function vault(
  enabled: boolean | null,
  effective: boolean,
  permission: string | null,
) {
  return {
    multimodal_provider_enabled: enabled,
    effective_multimodal_enabled: effective,
    current_user_permission: permission,
  };
}

function renderModels(
  formData: SettingsFormData,
  onChange: ModelsProps["onChange"] = vi.fn(),
  onClearKey: ModelsProps["onClearKey"] = vi.fn().mockResolvedValue(undefined),
  vaultId: number | null = null,
) {
  return render(
    <ModelsTab
      formData={formData}
      errors={{}}
      onChange={onChange}
      effectiveSources={{}}
      vaultId={vaultId}
      onClearKey={onClearKey}
    />,
  );
}

function renderCurator(
  formData: SettingsFormData,
  onChange: ComponentProps<typeof WikiCuratorSettings>["onChange"] = vi.fn(),
  vaultId: number | null = null,
) {
  return render(
    <WikiCuratorSettings
      formData={formData}
      errors={{}}
      onChange={onChange}
      vaultId={vaultId}
    />,
  );
}

function ControlledCurator({ initial }: { initial: SettingsFormData }) {
  const [formData, setFormData] = useState(initial);
  return (
    <WikiCuratorSettings
      formData={formData}
      errors={{}}
      onChange={(field, value) => {
        setFormData((current) => ({ ...current, [field]: value }) as SettingsFormData);
      }}
    />
  );
}

beforeEach(() => {
  authSnapshot = useAuthStore.getState();
  setAuth(userA, "jwt-a");
  vi.resetAllMocks();
  api.getVault.mockResolvedValue(vault(null, true, "admin"));
  api.toggleVaultMultimodalProvider.mockResolvedValue(vault(true, true, "admin"));
  api.testCuratorConnection.mockResolvedValue({ ok: true, model: "model-a", latency_ms: 12 });
  api.toggleVaultEnrichment.mockResolvedValue({
    enrichment_enabled: true,
    effective_enrichment_enabled: true,
    current_user_permission: "admin",
  });
});

afterEach(async () => {
  cleanup();
  reserveReplacementAuthOwner();
  await act(async () => {
    for (const settle of pendingSettlements) settle();
    await Promise.resolve();
  });
  act(() => useAuthStore.setState(authSnapshot, true));
});

describe("ModelsTab issue #775 lifecycle", () => {
  it("renders the actual vault tri-state and forwards an admin toggle", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderModels(baseFormData(), onChange, undefined, 42);

    await waitFor(() => expect(api.getVault).toHaveBeenCalledWith(42));
    expect(await screen.findByRole("radio", { name: /Inherit global/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: "On" })).toBeEnabled();
    await user.click(screen.getByRole("radio", { name: "On" }));

    await waitFor(() => expect(api.toggleVaultMultimodalProvider).toHaveBeenCalledWith(42, { enabled: true }));
  });

  it("uses the real permission field to disable a non-admin while effective state stays visible", async () => {
    api.getVault.mockResolvedValue(vault(false, true, "viewer"));
    renderModels(baseFormData(), undefined, undefined, 42);

    expect(await screen.findByRole("radio", { name: "Off" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "On" })).toBeDisabled();
    expect(screen.getByText("Only vault admins can change this.")).toBeInTheDocument();
  });

  it("shows a vault read error and recovers through Retry with a valid response", async () => {
    const user = userEvent.setup();
    api.getVault
      .mockRejectedValueOnce(new Error("read failed"))
      .mockResolvedValueOnce(vault(true, true, "admin"));
    renderModels(baseFormData(), undefined, undefined, 42);

    expect(await screen.findByText("read failed")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.getVault).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("radio", { name: "On" })).toBeChecked();
  });

  it("drops a retained old-owner vault read before the replacement owner can publish", async () => {
    const firstRead = deferred<ReturnType<typeof vault>>();
    const replacementRead = deferred<ReturnType<typeof vault>>();
    api.getVault.mockImplementationOnce(() => firstRead.promise).mockImplementationOnce(() => replacementRead.promise);
    renderModels(baseFormData(), undefined, undefined, 42);
    await waitFor(() => expect(api.getVault).toHaveBeenCalledWith(42));

    setAuth(userB, "jwt-b");
    await waitFor(() => expect(api.getVault).toHaveBeenCalledTimes(2));
    await resolveDeferred(firstRead, vault(false, false, "viewer"));
    expect(screen.queryByRole("radio", { name: "Off" })).not.toBeInTheDocument();

    await resolveDeferred(replacementRead, vault(true, true, "admin"));
    expect(await screen.findByRole("radio", { name: "On" })).toBeChecked();
  });

  it("gates duplicate clear dispatches and never publishes after unmount", async () => {
    const user = userEvent.setup();
    const heldClear = deferred<void>();
    const onChange = vi.fn();
    const onClearKey = vi.fn().mockReturnValue(heldClear.promise);
    const view = renderModels(baseFormData({ chat_api_key_set: true }), onChange, onClearKey);
    const button = await screen.findByRole("button", { name: "Clear thinking API key" });

    await user.click(button);
    expect(onClearKey).toHaveBeenCalledWith("chat_api_key");
    expect(onClearKey).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    await user.click(button);
    expect(onClearKey).toHaveBeenCalledTimes(1);

    view.unmount();
    reserveReplacementAuthOwner();
    await resolveDeferred(heldClear, undefined);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("exposes a clear failure and permits a successful retry", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onClearKey = vi.fn().mockRejectedValueOnce(new Error("denied")).mockResolvedValueOnce(undefined);
    renderModels(baseFormData({ chat_api_key_set: true }), onChange, onClearKey);
    const button = await screen.findByRole("button", { name: "Clear thinking API key" });

    await user.click(button);
    expect(await screen.findByText("Could not clear the API key. Retry to continue.")).toBeInTheDocument();
    await user.click(button);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith("chat_api_key", ""));
    expect(onClearKey).toHaveBeenCalledTimes(2);
  });
});

describe("WikiCuratorSettings issue #775 lifecycle", () => {
  it("dispatches the current URL/model and renders the real success response", async () => {
    const user = userEvent.setup();
    renderCurator(
      baseFormData({
        wiki_llm_curator_enabled: true,
        wiki_llm_curator_url: "https://curator-a.example",
        wiki_llm_curator_model: "model-a",
      }),
    );

    await user.click(screen.getByRole("button", { name: "Test curator connection" }));
    expect(api.testCuratorConnection).toHaveBeenCalledWith("https://curator-a.example", "model-a");
    expect(await screen.findByText(/OK.*12ms/)).toBeInTheDocument();
  });

  it("handles URL A/B/A with old finalizers ignored and a new test accepted", async () => {
    const user = userEvent.setup();
    const testA = deferred<{ ok: boolean; model: string; latency_ms: number }>();
    const testA2 = deferred<{ ok: boolean; model: string; latency_ms: number }>();
    api.testCuratorConnection.mockImplementationOnce(() => testA.promise).mockImplementationOnce(() => testA2.promise);
    render(<ControlledCurator initial={baseFormData({
      wiki_llm_curator_enabled: true,
      wiki_llm_curator_url: "https://curator-a.example",
      wiki_llm_curator_model: "model-a",
    })} />);

    await user.click(screen.getByRole("button", { name: "Test curator connection" }));
    expect(api.testCuratorConnection).toHaveBeenCalledWith("https://curator-a.example", "model-a");
    const url = screen.getByLabelText("Endpoint URL");
    await user.clear(url);
    await user.type(url, "https://curator-b.example");
    await waitFor(() => expect(screen.queryByText(/OK/)).not.toBeInTheDocument());
    await user.clear(url);
    await user.type(url, "https://curator-a.example");

    await user.click(screen.getByRole("button", { name: "Test curator connection" }));
    expect(api.testCuratorConnection).toHaveBeenCalledTimes(2);
    await resolveDeferred(testA, { ok: true, model: "model-a", latency_ms: 11 });
    expect(screen.queryByText(/OK/)).not.toBeInTheDocument();
    await resolveDeferred(testA2, { ok: true, model: "model-a", latency_ms: 13 });
    expect(await screen.findByText(/OK.*13ms/)).toBeInTheDocument();
  });

  it("recovers a malformed or failed enrichment read through Retry", async () => {
    const user = userEvent.setup();
    api.getVault
      .mockRejectedValueOnce(new Error("vault unavailable"))
      .mockResolvedValueOnce({
        enrichment_enabled: null,
        effective_enrichment_enabled: true,
        current_user_permission: "admin",
      });
    renderCurator(baseFormData(), undefined, 7);

    expect(await screen.findByText("vault unavailable")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.getVault).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("checkbox", { name: /Per-vault document enrichment/ })).toBeChecked();
  });
});
