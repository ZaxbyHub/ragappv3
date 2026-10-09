import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModelsTab } from "@/components/settings/ModelsTab";
import type { SettingsFormData } from "@/stores/useSettingsStore";
import { useSettingsStore } from "@/stores/useSettingsStore";
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

function baseFormData(overrides: Partial<SettingsFormData> = {}): SettingsFormData {
  return {
    ...useSettingsStore.getState().formData,
    chat_api_key: "",
    instant_api_key: "",
    ...overrides,
  } as SettingsFormData;
}

function renderTab(
  formData: SettingsFormData,
  onChange: ModelsTabProps["onChange"],
  onClearKey: ModelsTabProps["onClearKey"],
) {
  return render(
    <ModelsTab
      formData={formData}
      errors={{}}
      onChange={onChange}
      effectiveSources={{}}
      vaultId={null}
      onClearKey={onClearKey}
    />,
  );
}

type ModelsTabProps = ComponentProps<typeof ModelsTab>;

describe("ModelsTab issue #774 key owner guards", () => {
  beforeEach(() => {
    publicAuthSnapshot = useAuthStore.getState();
    setPublicAuthUser(publicUserA, "A-token");
    vi.resetAllMocks();
    mocks.getVault.mockResolvedValue({
      multimodal_provider_enabled: false,
      effective_multimodal_enabled: false,
      current_user_permission: "admin",
    });
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

  it.each([
    ["chat_api_key", "Clear thinking API key"],
    ["instant_api_key", "Clear instant API key"],
  ] as const)("clears the current %s through the original one-argument callback", async (field, label) => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onClearKey = vi.fn().mockResolvedValue(undefined);
    renderTab(
      baseFormData({ chat_api_key_set: true, instant_api_key_set: true }),
      onChange,
      onClearKey,
    );

    await user.click(screen.getByRole("button", { name: label }));

    await waitFor(() => expect(onClearKey).toHaveBeenCalledTimes(1));
    expect(onClearKey).toHaveBeenCalledWith(field);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(field, "");
    expect(screen.getByRole("button", { name: label })).not.toBeDisabled();
  });

  it("does not let a settled A clear publish into same-id replacement B or clear B busy state", async () => {
    const user = userEvent.setup();
    const clearA = deferred(undefined);
    const clearB = deferred(undefined);
    const onChange = vi.fn();
    const onClearKey = vi
      .fn<(field: "chat_api_key" | "instant_api_key") => Promise<void>>()
      .mockImplementationOnce(() => clearA.promise)
      .mockImplementationOnce(() => clearB.promise);
    renderTab(baseFormData({ chat_api_key_set: true }), onChange, onClearKey);

    await user.click(screen.getByRole("button", { name: "Clear thinking API key" }));
    expect(onClearKey).toHaveBeenCalledWith("chat_api_key");

    act(() => {
      setPublicAuthUser(publicUserSameAccount, "B-token");
    });

    const replacementClear = await screen.findByRole("button", { name: "Clear thinking API key" });
    await waitFor(() => expect(replacementClear).not.toBeDisabled());
    await user.click(replacementClear);
    expect(onClearKey).toHaveBeenCalledTimes(2);

    await act(async () => {
      clearA.resolve(undefined);
      await clearA.promise;
    });
    expect(onChange).not.toHaveBeenCalled();
    expect(replacementClear).toBeDisabled();

    await act(async () => {
      clearB.resolve(undefined);
      await clearB.promise;
    });
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    expect(onChange).toHaveBeenCalledWith("chat_api_key", "");
    expect(replacementClear).not.toBeDisabled();
  });

  it("rejects an old-render clear click immediately after owner replacement", async () => {
    const onChange = vi.fn();
    const onClearKey = vi.fn().mockResolvedValue(undefined);
    renderTab(baseFormData({ chat_api_key_set: true }), onChange, onClearKey);
    const oldRenderClear = await screen.findByRole("button", { name: "Clear thinking API key" });
    const oldRenderClearHandler = captureReactPropHandler(oldRenderClear, "onClick");

    act(() => {
      setPublicAuthUser(publicUserSameAccount, "B-token");
      oldRenderClearHandler();
    });

    expect(onClearKey).not.toHaveBeenCalled();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does not publish an unmounted clear completion", async () => {
    const clear = deferred(undefined);
    const onChange = vi.fn();
    const onClearKey = vi.fn().mockReturnValue(clear.promise);
    const view = renderTab(baseFormData({ chat_api_key_set: true }), onChange, onClearKey);

    await userEvent.setup().click(screen.getByRole("button", { name: "Clear thinking API key" }));
    view.unmount();
    await act(async () => {
      clear.resolve(undefined);
      await clear.promise;
    });

    expect(onClearKey).toHaveBeenCalledWith("chat_api_key");
    expect(onChange).not.toHaveBeenCalled();
  });
});
