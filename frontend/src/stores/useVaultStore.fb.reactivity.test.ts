// frontend/src/stores/useVaultStore.fb.reactivity.test.ts
// PR #835 feedback F-2 (external, execution-proven): selecting the store's
// stable getActiveVault FUNCTION never re-renders when `vaults` arrives or
// changes. The production fix derives the active vault via
// (s) => s.vaults.find((v) => v.id === s.activeVaultId); this pins that
// selector's reactivity against the REAL store: vaults arriving (or being
// renamed) with an unchanged activeVaultId must update the derived vault.

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useVaultStore } from "./useVaultStore";

describe("active-vault derived selector reactivity (fb F-2)", () => {
  beforeEach(() => {
    useVaultStore.setState({ vaults: [], activeVaultId: 7 });
  });

  afterEach(() => {
    useVaultStore.setState({ vaults: [], activeVaultId: null });
  });

  it("updates when vaults arrive after activeVaultId was set", () => {
    const { result } = renderHook(() =>
      useVaultStore((s) => s.vaults.find((v) => v.id === s.activeVaultId))
    );
    expect(result.current).toBeUndefined();

    act(() => {
      useVaultStore.setState({
        vaults: [{ id: 7, name: "Chat Vault", file_count: 3 }],
      });
    });

    expect(result.current?.name).toBe("Chat Vault");
    expect(result.current?.file_count).toBe(3);
  });

  it("updates when the active vault is renamed (activeVaultId unchanged)", () => {
    useVaultStore.setState({
      vaults: [{ id: 7, name: "Before", file_count: 1 }],
    });
    const { result } = renderHook(() =>
      useVaultStore((s) => s.vaults.find((v) => v.id === s.activeVaultId))
    );
    expect(result.current?.name).toBe("Before");

    act(() => {
      useVaultStore.setState({
        vaults: [{ id: 7, name: "After", file_count: 1 }],
      });
    });

    expect(result.current?.name).toBe("After");
  });

  it("clears when the matching vault disappears (activeVaultId unchanged)", () => {
    useVaultStore.setState({
      vaults: [{ id: 7, name: "Temp", file_count: 1 }],
    });
    const { result } = renderHook(() =>
      useVaultStore((s) => s.vaults.find((v) => v.id === s.activeVaultId))
    );
    expect(result.current).toBeDefined();

    act(() => {
      useVaultStore.setState({ vaults: [] });
    });

    expect(result.current).toBeUndefined();
  });
});
