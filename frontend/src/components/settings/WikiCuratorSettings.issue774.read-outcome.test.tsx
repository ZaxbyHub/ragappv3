import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WikiCuratorSettings } from "@/components/settings/WikiCuratorSettings";
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
  toggleVaultEnrichment: vi.fn(),
}));

const pendingSettlements = new Set<() => void>();

type ReactFiber = {
  memoizedProps?: unknown;
  return?: ReactFiber | null;
};

function captureCheckedChangeHandler(element: Element): (...args: unknown[]) => unknown {
  const carrier = element as unknown as Record<string, unknown>;
  const fiberKey = Object.keys(carrier).find((key) => key.startsWith("__reactFiber$"));
  let fiber = fiberKey ? (carrier[fiberKey] as ReactFiber | undefined) : undefined;
  while (fiber) {
    const props =
      fiber.memoizedProps && typeof fiber.memoizedProps === "object"
        ? (fiber.memoizedProps as Record<string, unknown>)
        : undefined;
    const handler = props?.onCheckedChange;
    if (typeof handler === "function") {
      return handler as (...args: unknown[]) => unknown;
    }
    fiber = fiber.return ?? undefined;
  }
  throw new Error("Missing React onCheckedChange handler");
}

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
    toggleVaultEnrichment: mocks.toggleVaultEnrichment,
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
    <WikiCuratorSettings
      formData={formData()}
      errors={{}}
      onChange={vi.fn()}
      vaultId={vaultId}
    />,
  );
}

describe("WikiCuratorSettings issue #774 read outcomes", () => {
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

  it("keeps an initial vault read pending instead of presenting a confirmed checkbox", () => {
    const read = deferred(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }));
    mocks.getVault.mockReturnValueOnce(read.promise);

    renderTab();

    expect(screen.getByRole("status")).toHaveTextContent(/Loading vault enrichment/i);
    expect(
      screen.queryByRole("checkbox", { name: /Per-vault document enrichment/i }),
    ).not.toBeInTheDocument();
    expect(mocks.toggleVaultEnrichment).not.toHaveBeenCalled();

    act(() => read.resolve(vault({ enrichment_enabled: false, effective_enrichment_enabled: false })));
  });

  it.each([
    [null, true, /No vault override — inherits global/i, "admin", false],
    [true, true, /Vault override: on/i, "read", true],
    [false, false, /Vault override: off/i, "read", true],
  ] as const)(
    "renders the authoritative enrichment result after Retry for %s permission",
    async (enabled, effective, copy, permission, readOnly) => {
      mocks.getVault
        .mockRejectedValueOnce(new Error("temporary read failure"))
        .mockResolvedValueOnce(
          vault({
            enrichment_enabled: enabled,
            effective_enrichment_enabled: effective,
            current_user_permission: permission,
          }),
        );

      renderTab();
      expect(await screen.findByRole("alert")).toHaveTextContent(/temporary read failure/i);
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));

      const control = await screen.findByRole("checkbox", {
        name: /Per-vault document enrichment/i,
      });
      if (effective) {
        expect(control).toBeChecked();
      } else {
        expect(control).not.toBeChecked();
      }
      if (readOnly) {
        expect(control).toBeDisabled();
      } else {
        expect(control).not.toBeDisabled();
      }
      expect(screen.getByText(copy)).toBeInTheDocument();
    },
  );

  it("clears same-id cached state and rereads after an auth-owner replacement", async () => {
    const first = deferred(vault({ enrichment_enabled: true, effective_enrichment_enabled: true }));
    const replacement = deferred(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }));
    mocks.getVault
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(replacement.promise);
    renderTab();

    act(() => first.resolve(vault({ enrichment_enabled: true, effective_enrichment_enabled: true })));
    expect(
      await screen.findByRole("checkbox", { name: /Per-vault document enrichment/i }),
    ).toBeChecked();

    setPublicAuthUser(publicUserSameAccount, "B-token");

    expect(await screen.findByRole("status")).toHaveTextContent(/Loading|Refreshing/i);
    expect(screen.queryByRole("checkbox", { name: /Per-vault document enrichment/i })).not.toBeInTheDocument();
    expect(mocks.getVault).toHaveBeenCalledTimes(2);
    act(() => replacement.resolve(vault({ enrichment_enabled: false, effective_enrichment_enabled: false })));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).not.toBeChecked());
  });

  it("keeps a successful B mutation when the obsolete A read settles", async () => {
    const oldRead = deferred(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }));
    const replacementRead = deferred(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }));
    const toggle = deferred(vault({ enrichment_enabled: true, effective_enrichment_enabled: true }));
    const followupRead = deferred(vault({ enrichment_enabled: true, effective_enrichment_enabled: true }));
    mocks.getVault
      .mockReturnValueOnce(oldRead.promise)
      .mockReturnValueOnce(replacementRead.promise)
      .mockReturnValueOnce(followupRead.promise);
    mocks.toggleVaultEnrichment.mockReturnValueOnce(toggle.promise);

    renderTab();
    setPublicAuthUser(publicUserSameAccount, "B-token");
    act(() => replacementRead.resolve(vault({ enrichment_enabled: false, effective_enrichment_enabled: false })));
    const control = await screen.findByRole("checkbox", { name: /Per-vault document enrichment/i });
    expect(control).not.toBeChecked();
    fireEvent.click(control);

    act(() => toggle.resolve(vault({ enrichment_enabled: true, effective_enrichment_enabled: true })));
    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(3));
    act(() => followupRead.resolve(vault({ enrichment_enabled: true, effective_enrichment_enabled: true })));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).toBeChecked());
    act(() => oldRead.resolve(vault({ enrichment_enabled: false, effective_enrichment_enabled: false })));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).toBeChecked());
  });

  it("rejects a stale A toggle after a same-id replacement and preserves B read state", async () => {
    const replacementRead = deferred(vault({ enrichment_enabled: true, effective_enrichment_enabled: true }));
    const toggleA = deferred(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }));
    mocks.getVault
      .mockResolvedValueOnce(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }))
      .mockReturnValueOnce(replacementRead.promise);
    mocks.toggleVaultEnrichment.mockReturnValueOnce(toggleA.promise);

    renderTab();
    const initial = await screen.findByRole("checkbox", { name: /Per-vault document enrichment/i });
    expect(initial).not.toBeChecked();
    fireEvent.click(initial);

    setPublicAuthUser(publicUserSameAccount, "B-token");
    expect(screen.queryByRole("checkbox", { name: /Per-vault document enrichment/i })).not.toBeInTheDocument();
    act(() => replacementRead.resolve(vault({ enrichment_enabled: true, effective_enrichment_enabled: true })));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).toBeChecked());

    act(() => toggleA.resolve(vault({ enrichment_enabled: false, effective_enrichment_enabled: false })));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).toBeChecked());
  });

  it("does not expose Retry or start another read while a toggle is pending", async () => {
    const toggle = deferred(vault({ enrichment_enabled: true, effective_enrichment_enabled: true }));
    const followupRead = deferred(vault({ enrichment_enabled: true, effective_enrichment_enabled: true }));
    mocks.getVault
      .mockResolvedValueOnce(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }))
      .mockReturnValueOnce(followupRead.promise);
    mocks.toggleVaultEnrichment.mockReturnValueOnce(toggle.promise);

    renderTab();
    const control = await screen.findByRole("checkbox", { name: /Per-vault document enrichment/i });
    fireEvent.click(control);

    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).toBeDisabled());
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(mocks.getVault).toHaveBeenCalledTimes(1);
    act(() => toggle.resolve(vault({ enrichment_enabled: true, effective_enrichment_enabled: true })));
    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(2));
    act(() => followupRead.resolve(vault({ enrichment_enabled: true, effective_enrichment_enabled: true })));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).toBeChecked());
  });

  it("retains the last confirmed state and exposes Retry when toggle recovery also fails", async () => {
    mocks.getVault
      .mockResolvedValueOnce(vault({ enrichment_enabled: true, effective_enrichment_enabled: true }))
      .mockRejectedValueOnce(new Error("refresh failed"))
      .mockResolvedValueOnce(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }));
    mocks.toggleVaultEnrichment.mockRejectedValueOnce(new Error("toggle failed"));

    renderTab();
    const control = await screen.findByRole("checkbox", { name: /Per-vault document enrichment/i });
    expect(control).toBeChecked();
    fireEvent.click(control);

    expect(await screen.findByRole("alert")).toHaveTextContent(/refresh failed/i);
    expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).not.toBeChecked());
  });

  it("ignores a late same-context Retry result after a successful toggle and follow-up read", async () => {
    const lateRetry = deferred(vault({ enrichment_enabled: true, effective_enrichment_enabled: true }));
    const followupRead = deferred(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }));
    const secondToggle = deferred(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }));
    mocks.getVault
      .mockResolvedValueOnce(vault({ enrichment_enabled: true, effective_enrichment_enabled: true }))
      .mockRejectedValueOnce(new Error("refresh failed"))
      .mockReturnValueOnce(lateRetry.promise)
      .mockReturnValueOnce(followupRead.promise);
    mocks.toggleVaultEnrichment
      .mockRejectedValueOnce(new Error("toggle failed"))
      .mockReturnValueOnce(secondToggle.promise);

    renderTab();
    const initial = await screen.findByRole("checkbox", { name: /Per-vault document enrichment/i });
    expect(initial).toBeChecked();
    fireEvent.click(initial);

    expect(await screen.findByRole("alert")).toHaveTextContent(/refresh failed/i);
    expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(3));

    fireEvent.click(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i }));
    await waitFor(() => expect(mocks.toggleVaultEnrichment).toHaveBeenCalledTimes(2));
    act(() => secondToggle.resolve(vault({ enrichment_enabled: false, effective_enrichment_enabled: false })));
    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(4));
    act(() => followupRead.resolve(vault({ enrichment_enabled: false, effective_enrichment_enabled: false })));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).not.toBeChecked());

    act(() => lateRetry.resolve(vault({ enrichment_enabled: true, effective_enrichment_enabled: true })));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).not.toBeChecked());
  });

  it("rejects a toggle from the old same-id owner render before React commits replacement", async () => {
    const replacement = deferred(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }));
    mocks.getVault
      .mockResolvedValueOnce(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }))
      .mockReturnValueOnce(replacement.promise);
    renderTab();
    const oldRenderControl = await screen.findByRole("checkbox", { name: /Per-vault document enrichment/i });
    const oldRenderToggle = captureCheckedChangeHandler(oldRenderControl);
    expect(mocks.getVault).toHaveBeenCalledTimes(1);

    act(() => {
      setPublicAuthUser(publicUserSameAccount, "B-token");
      const callsBeforeOldHandler = mocks.getVault.mock.calls.length;
      oldRenderToggle(true);
      expect(mocks.getVault).toHaveBeenCalledTimes(callsBeforeOldHandler);
    });

    expect(mocks.toggleVaultEnrichment).not.toHaveBeenCalled();
    await waitFor(() => expect(mocks.getVault).toHaveBeenCalledTimes(2));
    act(() => replacement.resolve(vault({ enrichment_enabled: false, effective_enrichment_enabled: false })));
  });

  it("rejects Retry from the old same-id owner render before React commits replacement", async () => {
    const replacement = deferred(vault({ enrichment_enabled: false, effective_enrichment_enabled: false }));
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
    act(() => replacement.resolve(vault({ enrichment_enabled: false, effective_enrichment_enabled: false })));
  });

  it("rejects an older vault read after switching context", async () => {
    const oldRead = deferred(vault({ id: 7, effective_enrichment_enabled: true }));
    const newRead = deferred(vault({ id: 8, effective_enrichment_enabled: false }));
    mocks.getVault
      .mockReturnValueOnce(oldRead.promise)
      .mockReturnValueOnce(newRead.promise);
    const view = renderTab(7);

    view.rerender(
      <WikiCuratorSettings
        formData={formData()}
        errors={{}}
        onChange={vi.fn()}
        vaultId={8}
      />,
    );
    act(() => oldRead.resolve(vault({ id: 7, effective_enrichment_enabled: true })));
    expect(await screen.findByRole("status")).toHaveTextContent(/Loading|Refreshing/i);
    act(() => newRead.resolve(vault({ id: 8, effective_enrichment_enabled: false })));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).not.toBeChecked());
  });

  it("does not let an unmounted read finalizer affect a later mount", async () => {
    const staleRead = deferred(vault({ effective_enrichment_enabled: true }));
    mocks.getVault
      .mockReturnValueOnce(staleRead.promise)
      .mockResolvedValueOnce(vault({ effective_enrichment_enabled: false }));
    const first = renderTab();
    first.unmount();
    act(() => staleRead.resolve(vault({ effective_enrichment_enabled: true })));

    renderTab();
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Per-vault document enrichment/i })).not.toBeChecked());
  });

  it("does not issue a vault read for the no-vault/demo surface", () => {
    renderTab(null);
    expect(screen.queryByText(/Per-vault document enrichment/i)).not.toBeInTheDocument();
    expect(mocks.getVault).not.toHaveBeenCalled();
  });

  it("keeps a confirmed read-only state visible while disabling the checkbox", async () => {
    mocks.getVault.mockResolvedValueOnce(
      vault({ current_user_permission: "read", effective_enrichment_enabled: true }),
    );
    renderTab();

    const control = await screen.findByRole("checkbox", { name: /Per-vault document enrichment/i });
    expect(control).toBeChecked();
    expect(control).toBeDisabled();
  });
});
