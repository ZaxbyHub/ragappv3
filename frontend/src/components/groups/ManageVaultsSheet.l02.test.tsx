import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManageVaultsSheet } from "./ManageVaultsSheet";
import type { Group, Vault, VaultAccessItem } from "@/lib/api";

const { listVaultsMock, getGroupVaultsMock } = vi.hoisted(() => ({
  listVaultsMock: vi.fn(),
  getGroupVaultsMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listVaults: (...args: unknown[]) => listVaultsMock(...args),
  getGroupVaults: (...args: unknown[]) => getGroupVaultsMock(...args),
}));

vi.mock("@/components/ui/sheet", () => ({
  Sheet: ({ children, open }: { children: ReactNode; open: boolean }) =>
    open ? <div>{children}</div> : null,
  SheetContent: ({ children, ...props }: { children: ReactNode }) => (
    <div {...props}>{children}</div>
  ),
  SheetDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  SheetFooter: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SheetHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SheetTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
}));
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: { children: ReactNode }) => (
    <button {...props}>{children}</button>
  ),
}));
vi.mock("@/components/ui/input", () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));
vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({ onCheckedChange, ...props }: { onCheckedChange?: () => void }) => (
    <input type="checkbox" {...props} onChange={() => onCheckedChange?.()} />
  ),
}));
vi.mock("@/components/ui/label", () => ({
  Label: ({ children, ...props }: { children: ReactNode }) => <label {...props}>{children}</label>,
}));
vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/ui/skeleton", () => ({ Skeleton: () => <div /> }));
vi.mock("@/components/ui/select", () => ({
  Select: ({ children, disabled }: { children: ReactNode; disabled?: boolean }) => (
    <div role="combobox" aria-disabled={disabled ? "true" : "false"}>{children}</div>
  ),
  SelectContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SelectItem: ({ children }: { children: ReactNode }) => <option>{children}</option>,
  SelectTrigger: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SelectValue: () => <span />,
}));
vi.mock("lucide-react", () => ({
  FolderOpen: () => <span />,
  Loader2: () => <span />,
  Search: () => <span />,
  Shield: () => <span />,
}));

function makeGroup(): Group {
  return {
    id: 7,
    name: "Editors",
    description: null,
    created_at: "2024-01-01T00:00:00Z",
    org_id: 1,
    organization_name: "Acme",
  };
}

function makeVault(id = 101, name = "Knowledge Vault"): Vault {
  return {
    id,
    name,
    description: null,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    file_count: 0,
    memory_count: 0,
    session_count: 0,
    org_id: 1,
    current_user_permission: "admin",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderSheet(onSave: (access: VaultAccessItem[]) => Promise<void>, editorToken = 1) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <ManageVaultsSheet
        group={makeGroup()}
        open
        editorToken={editorToken}
        onOpenChange={vi.fn()}
        onSave={onSave}
      />
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

describe("ManageVaultsSheet issue #772 edge coverage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listVaultsMock.mockResolvedValue({ vaults: [makeVault()] });
    getGroupVaultsMock.mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps Save disabled when the vault catalog read fails", async () => {
    listVaultsMock.mockRejectedValue(new Error("vault catalog unavailable"));
    const { queryClient } = renderSheet(vi.fn().mockResolvedValue(undefined));

    await waitFor(() => expect(queryClient.getQueryState(["vaults"])?.status).toBe("error"));
    expect(screen.getByText(/unable to load vault access/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save vault access changes" })).toBeDisabled();
  });

  it("allows an empty replacement after an empty catalog and access read succeed", async () => {
    listVaultsMock.mockResolvedValue({ vaults: [] });
    const onSave = vi.fn<(access: VaultAccessItem[]) => Promise<void>>().mockResolvedValue();
    renderSheet(onSave);

    const save = await screen.findByRole("button", { name: "Save vault access changes" });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);

    await waitFor(() => expect(onSave).toHaveBeenCalledWith([]));
  });

  it("waits for cached opening reads before enabling access edits and preserves a later local edit", async () => {
    const cachedVault = makeVault(101, "Cached Vault");
    const freshVault = makeVault(202, "Fresh Vault");
    const cachedAccess = { id: cachedVault.id, name: cachedVault.name, org_id: 1, permission: "read" };
    const freshAccess = { id: freshVault.id, name: freshVault.name, org_id: 1, permission: "write" };
    const catalogRead = deferred<Vault[]>();
    const accessRead = deferred<Array<typeof cachedAccess>>();
    const backgroundRead = deferred<Array<typeof freshAccess>>();
    const onSave = vi.fn<(access: VaultAccessItem[]) => Promise<void>>().mockResolvedValue(undefined);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0 } },
    });
    const catalogKey = ["vaults"];
    const accessKey = ["groups", 7, "vaults"];

    queryClient.setQueryData(catalogKey, [cachedVault, freshVault]);
    queryClient.setQueryData(accessKey, [cachedAccess]);
    listVaultsMock.mockReturnValue(catalogRead.promise.then((vaults) => ({ vaults })));
    getGroupVaultsMock.mockReturnValue(accessRead.promise);

    render(
      <QueryClientProvider client={queryClient}>
        <ManageVaultsSheet
          group={makeGroup()}
          open
          editorToken={1}
          onOpenChange={vi.fn()}
          onSave={onSave}
        />
      </QueryClientProvider>,
    );

    const freshCheckbox = await screen.findByLabelText("Grant access to Fresh Vault");
    const save = screen.getByRole("button", { name: "Save vault access changes" });
    const permissionSelects = screen.getAllByRole("combobox");
    expect(freshCheckbox).toBeDisabled();
    expect(save).toBeDisabled();
    expect(permissionSelects.every((control) => control.getAttribute("aria-disabled") === "true")).toBe(true);
    await waitFor(() => {
      expect(queryClient.getQueryState(catalogKey)?.fetchStatus).toBe("fetching");
      expect(queryClient.getQueryState(accessKey)?.fetchStatus).toBe("fetching");
    });

    await act(async () => {
      catalogRead.resolve([cachedVault, freshVault]);
      accessRead.resolve([freshAccess]);
      await Promise.all([catalogRead.promise, accessRead.promise]);
    });
    await waitFor(() => expect(save).not.toBeDisabled());
    expect(freshCheckbox).toBeChecked();
    expect(screen.getAllByRole("combobox")[1]).toHaveAttribute("aria-disabled", "false");

    fireEvent.click(freshCheckbox);
    expect(freshCheckbox).not.toBeChecked();
    getGroupVaultsMock.mockReturnValueOnce(backgroundRead.promise);
    const backgroundRefetch = queryClient.refetchQueries({ queryKey: accessKey });
    await waitFor(() => expect(queryClient.getQueryState(accessKey)?.fetchStatus).toBe("fetching"));
    expect(freshCheckbox).not.toBeDisabled();
    expect(save).toBeDisabled();

    await act(async () => {
      backgroundRead.resolve([freshAccess]);
      await backgroundRefetch;
    });
    await waitFor(() => expect(queryClient.getQueryState(accessKey)?.fetchStatus).toBe("idle"));
    expect(freshCheckbox).not.toBeChecked();
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);
    await waitFor(() => expect(onSave).toHaveBeenCalledWith([]));
  });

  it("keeps a reopened editor ready after an older save resolves", async () => {
    const firstSave = deferred<void>();
    const onSave = vi.fn<(access: VaultAccessItem[]) => Promise<void>>().mockReturnValue(firstSave.promise);
    let view: ReturnType<typeof renderSheet> | undefined;
    try {
      view = renderSheet(onSave, 1);
      const initialSave = await screen.findByRole("button", { name: "Save vault access changes" });
      await waitFor(() => expect(initialSave).not.toBeDisabled());
      fireEvent.click(initialSave);
      await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));

      view.rerender(
        <QueryClientProvider client={view.queryClient}>
          <ManageVaultsSheet
            group={makeGroup()}
            open
            editorToken={2}
            onOpenChange={vi.fn()}
            onSave={onSave}
          />
        </QueryClientProvider>,
      );
      const reopenedSave = await screen.findByRole("button", { name: "Save vault access changes" });
      await waitFor(() => expect(reopenedSave).not.toBeDisabled());

      await act(async () => {
        firstSave.resolve();
        await firstSave.promise;
        await Promise.resolve();
      });
      expect(reopenedSave).not.toBeDisabled();
    } finally {
      await act(async () => {
        firstSave.resolve();
        await firstSave.promise;
      });
      view?.queryClient.clear();
      view?.unmount();
    }
  });

  it("contains a current save failure so the editor can retry", async () => {
    const onSave = vi
      .fn<(access: VaultAccessItem[]) => Promise<void>>()
      .mockRejectedValueOnce(new Error("vault save unavailable"))
      .mockResolvedValueOnce(undefined);
    renderSheet(onSave);

    const save = await screen.findByRole("button", { name: "Save vault access changes" });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(save).not.toBeDisabled());

    fireEvent.click(save);
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
  });

  it("does not let an older rejected save clear a newer opening's pending save", async () => {
    const firstSave = deferred<void>();
    const secondSave = deferred<void>();
    const onSave = vi
      .fn<(access: VaultAccessItem[]) => Promise<void>>()
      .mockReturnValueOnce(firstSave.promise)
      .mockReturnValueOnce(secondSave.promise);
    let view: ReturnType<typeof renderSheet> | undefined;
    try {
      view = renderSheet(onSave, 1);

      const firstButton = await screen.findByRole("button", { name: "Save vault access changes" });
      await waitFor(() => expect(firstButton).not.toBeDisabled());
      fireEvent.click(firstButton);
      await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));

      view.rerender(
        <QueryClientProvider client={view.queryClient}>
          <ManageVaultsSheet
            group={makeGroup()}
            open
            editorToken={2}
            onOpenChange={vi.fn()}
            onSave={onSave}
          />
        </QueryClientProvider>,
      );
      const reopenedButton = await screen.findByRole("button", { name: "Save vault access changes" });
      await waitFor(() => expect(reopenedButton).not.toBeDisabled());
      fireEvent.click(reopenedButton);
      await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
      expect(reopenedButton).toBeDisabled();

      await act(async () => {
        firstSave.reject(new Error("older vault save failed"));
        await Promise.allSettled([firstSave.promise]);
        await Promise.resolve();
      });
      expect(reopenedButton).toBeDisabled();

      await act(async () => {
        secondSave.resolve();
        await secondSave.promise;
      });
      expect(reopenedButton).not.toBeDisabled();
    } finally {
      await act(async () => {
        firstSave.reject(new Error("older vault save failed"));
        secondSave.resolve();
        await Promise.allSettled([firstSave.promise, secondSave.promise]);
      });
      view?.queryClient.clear();
      view?.unmount();
    }
  });
});
