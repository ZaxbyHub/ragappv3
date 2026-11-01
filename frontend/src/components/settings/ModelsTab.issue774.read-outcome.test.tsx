import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ModelsTab } from "@/components/settings/ModelsTab";
import type { Vault } from "@/lib/api/vaults";
import { useAuthStore } from "@/stores/useAuthStore";

let publicAuthSnapshot: ReturnType<typeof useAuthStore.getState>;
const publicUserA = {
  id: 101,
  username: "owner-a",
  full_name: "Owner A",
  role: "member" as const,
  is_active: true,
};
const publicUserSameAccount = {
  id: 101,
  username: "owner-a",
  full_name: "Owner A",
  role: "member" as const,
  is_active: true,
};

function setPublicAuthUser(user: typeof publicUserA, token: string) {
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

function restorePublicAuthState() {
  act(() => useAuthStore.setState(publicAuthSnapshot, true));
}
import { useSettingsStore } from "@/stores/useSettingsStore";
import type { SettingsFormData } from "@/stores/useSettingsStore";

const mocks = vi.hoisted(() => ({
  getVault: vi.fn(),
  toggleVaultMultimodalProvider: vi.fn(),
}));

const pendingSettlements = new Set<() => void>();

function captureReactPropHandler(element: Element, prop: string): (...args: unknown[]) => unknown {
  const carrier = element as unknown as Record<string, unknown>;
  const propsKey = Object.keys(carrier).find((key) => key.startsWith("__reactProps$"));
  const props = propsKey ? (carrier[propsKey] as Record<string, unknown>) : undefined;
  const handler = props?.[prop];
  if (typeof handler !== "function") {
    throw new Error(`Missing React prop handler ${prop}`);
  }
  return handler as (...args: unknown[]) => unknown;
}

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getVault: mocks.getVault,
    toggleVaultMultimodalProvider: mocks.toggleVaultMultimodalProvider,
  };
});


function deferred<T>(fallback: T) {
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
  const cleanupSettlement = () => resolve(fallback);
  pendingSettlements.add(cleanupSettlement);
  return { promise, resolve, reject };
}

function vault(overrides: Partial<Vault> = {}): Vault {
  return {
    id: 7,
    name: "Vault",
    description: "",
    created_at: "",
    updated_at: "",
    file_count: 0,
    memory_count: 0,
    session_count: 0,
    org_id: null,
    current_user_permission: "admin",
    enrichment_enabled: null,
    effective_enrichment_enabled: false,
    multimodal_provider_enabled: null,
    effective_multimodal_enabled: false,
    ...overrides,
  };
}

function formData(): SettingsFormData {
  return { ...useSettingsStore.getState().formData };
}

function renderTab(vaultId: number | null = 7) {
  return render(
    <ModelsTab
      formData={formData()}
      errors={{}}
      onChange={vi.fn()}
      effectiveSources={{}}
      vaultId={vaultId}
    />,
  );
}

describe("ModelsTab issue #774 read outcomes", () => {
  beforeEach(() => {
    publicAuthSnapshot = useAuthStore.getState();
    setPublicAuthUser(publicUserA, "A-token");
    vi.resetAllMocks();
  });

  afterEach(async () => {
    try {
      cleanup();
      await act(async () => {
        for (const settle of pendingSettlements) settle();
        await Promise.resolve();
      });
    } finally {
      restorePublicAuthState();
    }
  });

  it("keeps an initial vault read pending instead of presenting a confirmed off control", () => {
    const read = deferred(vault({ multimodal_provider_enabled: false }));
    mocks.getVault.mockReturnValueOnce(read.promise);

    renderTab();

    expect(screen.getByRole("status")).toHaveTextContent(/Loading vault multimodal/i);
    expect(screen.queryByLabelText("On")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Off")).not.toBeInTheDocument();
    expect(mocks.toggleVaultMultimodalProvider).not.toHaveBeenCalled();

    act(() => read.resolve(vault({ multimodal_provider_enabled: false })));
  });

  it.each([
    [null, "Inherit global"],
    [true, "On"],
    [false, "Off"],
  ] as const)("renders the authoritative %s result after a Retry", async (enabled, label) => {
    mocks.getVault
      .mockRejectedValueOnce(new Error("temporary read failure"))
      .mockResolvedValueOnce(vault({ multimodal_provider_enabled: enabled }));

    renderTab();
    const diagnostic = await screen.findByText(/temporary read failure/i);
    expect(diagnostic.closest('[role="alert"]')).toHaveTextContent(/temporary read failure/i);

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("radio", {
        name: label === "Inherit global" ? /^Inherit global\b/i : new RegExp(`^${label}$`, "i"),
      }),
    ).toBeChecked();
  });

  it("clears same-id cached state and rereads after an auth-owner replacement", async () => {
    const first = deferred(vault({ multimodal_provider_enabled: true }));
    const replacement = deferred(vault({ multimodal_provider_enabled: false }));
    mocks.getVault
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(replacement.promise);
    renderTab();

    act(() => first.resolve(vault({ multimodal_provider_enabled: true })));
    expect(await screen.findByLabelText("On")).toBeChecked();

    setPublicAuthUser(publicUserSameAccount, "B-token");

    expect(await screen.findByRole("status")).toHaveTextContent(/Loading|Refreshing/i);
    expect(screen.queryByLabelText("On")).not.toBeInTheDocument();
    expect(mocks.getVault).toHaveBeenCalledTimes(2);
    act(() => replacement.resolve(vault({ multimodal_provider_enabled: false })));
    await waitFor(() => expect(screen.getByLabelText("Off")).toBeChecked());
  });

  it("keeps a successful B mutation when the obsolete A read settles", async () => {
    const oldRead = deferred(vault({ multimodal_provider_enabled: false }));
    const replacementRead = deferred(vault({ multimodal_provider_enabled: false }));
    const toggle = deferred(vault({ multimodal_provider_enabled: true }));
    const followupRead = deferred(vault({ multimodal_provider_enabled: true }));
    mocks.getVault
      .mockReturnValueOnce(oldRead.promise)
      .mockReturnValueOnce(replacementRead.promise)
      .mockReturnValueOnce(followupRead.promise);
    mocks.toggleVaultMultimodalProvider.mockReturnValueOnce(toggle.promise);

    renderTab();
    setPublicAuthUser(publicUserSameAccount, "B-token");
    act(() => replacementRead.resolve(vault({ multimodal_provider_enabled: false })));
    await waitFor(() => expect(screen.getByLabelText("Off")).toBeChecked());

    fireEvent.click(screen.getByLabelText("On"));
    act(() => toggle.resolve(vault({ multimodal_provider_enabled: true })));
    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(3));
    act(() => followupRead.resolve(vault({ multimodal_provider_enabled: true })));
    await waitFor(() => expect(screen.getByLabelText("On")).toBeChecked());

    act(() => oldRead.resolve(vault({ multimodal_provider_enabled: false })));
    await waitFor(() => expect(screen.getByLabelText("On")).toBeChecked());
    expect(screen.getByLabelText("Off")).not.toBeChecked();
  });

  it("rejects a stale A toggle after a same-id replacement and preserves B read state", async () => {
    const replacementRead = deferred(vault({ multimodal_provider_enabled: true }));
    const toggleA = deferred(vault({ multimodal_provider_enabled: false }));
    mocks.getVault
      .mockResolvedValueOnce(vault({ multimodal_provider_enabled: false }))
      .mockReturnValueOnce(replacementRead.promise);
    mocks.toggleVaultMultimodalProvider.mockReturnValueOnce(toggleA.promise);

    renderTab();
    await screen.findByLabelText("Off");
    fireEvent.click(screen.getByLabelText("On"));

    setPublicAuthUser(publicUserSameAccount, "B-token");
    expect(screen.queryByLabelText("Off")).not.toBeInTheDocument();
    act(() => replacementRead.resolve(vault({ multimodal_provider_enabled: true })));
    await waitFor(() => expect(screen.getByLabelText("On")).toBeChecked());

    act(() => toggleA.resolve(vault({ multimodal_provider_enabled: false })));
    await waitFor(() => expect(screen.getByLabelText("On")).toBeChecked());
    expect(screen.getByLabelText("Off")).not.toBeChecked();
  });

  it("does not expose Retry or start another read while a toggle is pending", async () => {
    const toggle = deferred(vault({ multimodal_provider_enabled: true }));
    const followupRead = deferred(vault({ multimodal_provider_enabled: true }));
    mocks.getVault
      .mockResolvedValueOnce(vault({ multimodal_provider_enabled: false }))
      .mockReturnValueOnce(followupRead.promise);
    mocks.toggleVaultMultimodalProvider.mockReturnValueOnce(toggle.promise);

    renderTab();
    await screen.findByLabelText("Off");
    fireEvent.click(screen.getByLabelText("On"));

    await waitFor(() => expect(screen.getByLabelText("On")).toBeDisabled());
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(mocks.getVault).toHaveBeenCalledTimes(1);
    act(() => toggle.resolve(vault({ multimodal_provider_enabled: true })));
    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(2));
    act(() => followupRead.resolve(vault({ multimodal_provider_enabled: true })));
    await waitFor(() => expect(screen.getByLabelText("On")).toBeChecked());
  });

  it("retains the last confirmed state and exposes Retry when toggle recovery also fails", async () => {
    mocks.getVault
      .mockResolvedValueOnce(vault({ multimodal_provider_enabled: true }))
      .mockRejectedValueOnce(new Error("refresh failed"))
      .mockResolvedValueOnce(vault({ multimodal_provider_enabled: false }));
    mocks.toggleVaultMultimodalProvider.mockRejectedValueOnce(new Error("toggle failed"));

    renderTab();
    expect(await screen.findByLabelText("On")).toBeChecked();
    fireEvent.click(screen.getByLabelText("Off"));

    const diagnostic = await screen.findByText(/refresh failed/i);
    expect(diagnostic.closest('[role="alert"]')).toHaveTextContent(/refresh failed/i);
    expect(screen.getByLabelText("On")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByLabelText("Off")).toBeChecked());
  });

  it("ignores a late same-context Retry result after a successful toggle and follow-up read", async () => {
    const lateRetry = deferred(vault({ multimodal_provider_enabled: true }));
    const followupRead = deferred(vault({ multimodal_provider_enabled: false }));
    const secondToggle = deferred(vault({ multimodal_provider_enabled: false }));
    mocks.getVault
      .mockResolvedValueOnce(vault({ multimodal_provider_enabled: true }))
      .mockRejectedValueOnce(new Error("refresh failed"))
      .mockReturnValueOnce(lateRetry.promise)
      .mockReturnValueOnce(followupRead.promise);
    mocks.toggleVaultMultimodalProvider
      .mockRejectedValueOnce(new Error("toggle failed"))
      .mockReturnValueOnce(secondToggle.promise);

    renderTab();
    expect(await screen.findByLabelText("On")).toBeChecked();
    fireEvent.click(screen.getByLabelText("Off"));

    const diagnostic = await screen.findByText(/refresh failed/i);
    expect(diagnostic.closest('[role="alert"]')).toHaveTextContent(/refresh failed/i);
    expect(screen.getByLabelText("On")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(3));

    fireEvent.click(screen.getByLabelText("Off"));
    await waitFor(() => expect(mocks.toggleVaultMultimodalProvider).toHaveBeenCalledTimes(2));
    act(() => secondToggle.resolve(vault({ multimodal_provider_enabled: false })));
    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(4));
    act(() => followupRead.resolve(vault({ multimodal_provider_enabled: false })));
    await waitFor(() => expect(screen.getByLabelText("Off")).toBeChecked());

    act(() => lateRetry.resolve(vault({ multimodal_provider_enabled: true })));
    await waitFor(() => expect(screen.getByLabelText("Off")).toBeChecked());
    expect(screen.getByLabelText("On")).not.toBeChecked();
  });

  it("rejects a toggle from the old same-id owner render before React commits replacement", async () => {
    const replacement = deferred(vault({ multimodal_provider_enabled: false }));
    mocks.getVault
      .mockResolvedValueOnce(vault({ multimodal_provider_enabled: false }))
      .mockReturnValueOnce(replacement.promise);
    renderTab();
    const oldRenderControl = await screen.findByLabelText("On");
    const oldRenderToggle = captureReactPropHandler(oldRenderControl, "onChange");
    expect(mocks.getVault).toHaveBeenCalledTimes(1);

    act(() => {
      setPublicAuthUser(publicUserSameAccount, "B-token");
      const callsBeforeOldHandler = mocks.getVault.mock.calls.length;
      oldRenderToggle();
      expect(mocks.getVault).toHaveBeenCalledTimes(callsBeforeOldHandler);
    });

    expect(mocks.toggleVaultMultimodalProvider).not.toHaveBeenCalled();
    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(2));
    act(() => replacement.resolve(vault({ multimodal_provider_enabled: false })));
  });

  it("rejects Retry from the old same-id owner render before React commits replacement", async () => {
    const replacement = deferred(vault({ multimodal_provider_enabled: false }));
    mocks.getVault
      .mockRejectedValueOnce(new Error("initial read failed"))
      .mockReturnValueOnce(replacement.promise);
    renderTab();
    const oldRenderRetry = await screen.findByRole("button", { name: "Retry" });
    const oldRenderRetryHandler = captureReactPropHandler(oldRenderRetry, "onClick");
    expect(mocks.getVault).toHaveBeenCalledTimes(1);

    act(() => {
      setPublicAuthUser(publicUserSameAccount, "B-token");
      const callsBeforeOldHandler = mocks.getVault.mock.calls.length;
      oldRenderRetryHandler();
      expect(mocks.getVault).toHaveBeenCalledTimes(callsBeforeOldHandler);
    });

    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(2));
    act(() => replacement.resolve(vault({ multimodal_provider_enabled: false })));
  });

  it("rejects an older vault read after switching context", async () => {
    const oldRead = deferred(vault({ id: 7, multimodal_provider_enabled: true }));
    const newRead = deferred(vault({ id: 8, multimodal_provider_enabled: false }));
    mocks.getVault
      .mockReturnValueOnce(oldRead.promise)
      .mockReturnValueOnce(newRead.promise);
    const view = renderTab(7);

    view.rerender(
      <ModelsTab
        formData={formData()}
        errors={{}}
        onChange={vi.fn()}
        effectiveSources={{}}
        vaultId={8}
      />,
    );
    act(() => oldRead.resolve(vault({ id: 7, multimodal_provider_enabled: true })));
    expect(await screen.findByRole("status")).toHaveTextContent(/Loading|Refreshing/i);
    act(() => newRead.resolve(vault({ id: 8, multimodal_provider_enabled: false })));
    await waitFor(() => expect(screen.getByLabelText("Off")).toBeChecked());
  });

  it("does not let an unmounted read finalizer affect a later mount", async () => {
    const staleRead = deferred(vault({ multimodal_provider_enabled: true }));
    mocks.getVault
      .mockReturnValueOnce(staleRead.promise)
      .mockResolvedValueOnce(vault({ multimodal_provider_enabled: false }));
    const first = renderTab();
    first.unmount();
    act(() => staleRead.resolve(vault({ multimodal_provider_enabled: true })));

    renderTab();
    await waitFor(() => expect(screen.getByLabelText("Off")).toBeChecked());
  });

  it("does not issue a vault read for the no-vault/demo surface", () => {
    renderTab(null);
    expect(screen.queryByText(/Multimodal provider opt-in \(this vault\)/i)).not.toBeInTheDocument();
    expect(mocks.getVault).not.toHaveBeenCalled();
  });

  it("keeps a confirmed read-only state visible while disabling the radios", async () => {
    mocks.getVault.mockResolvedValueOnce(
      vault({ current_user_permission: "read", multimodal_provider_enabled: true }),
    );
    renderTab();

    const on = await screen.findByLabelText("On");
    expect(on).toBeChecked();
    expect(on).toBeDisabled();
    expect(screen.getByLabelText("Off")).toBeDisabled();
  });
});
