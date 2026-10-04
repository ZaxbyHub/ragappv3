import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Group, VaultAccessItem } from "@/lib/api";
import AdminGroupsPage from "./AdminGroupsPage";

const { updateGroupMembersMock, updateGroupVaultsMock } = vi.hoisted(() => ({
  updateGroupMembersMock: vi.fn(),
  updateGroupVaultsMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createGroup: vi.fn(),
  updateGroup: vi.fn(),
  deleteGroup: vi.fn(),
  updateGroupMembers: updateGroupMembersMock,
  updateGroupVaults: updateGroupVaultsMock,
}));

vi.mock("@/components/auth/RoleGuard", () => ({
  AdminGuard: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: { children: ReactNode }) => <button {...props}>{children}</button>,
}));
vi.mock("@/components/layout/PageTitleHeader", () => ({
  PageTitleHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));
vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children, open }: { children: ReactNode; open: boolean }) => open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
}));
vi.mock("lucide-react", () => ({ Plus: () => <span /> }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/components/groups/GroupTable", () => ({
  GroupTable: ({
    onManageMembers,
    onManageVaults,
  }: {
    onManageMembers: (group: Group) => void;
    onManageVaults: (group: Group) => void;
  }) => (
    <div>
      <button onClick={() => onManageMembers(group(1))}>Manage group A members</button>
      <button onClick={() => onManageMembers(group(2))}>Manage group B members</button>
      <button onClick={() => onManageVaults(group(1))}>Manage group A vaults</button>
      <button onClick={() => onManageVaults(group(2))}>Manage group B vaults</button>
    </div>
  ),
}));
vi.mock("@/components/groups/GroupFormDialog", () => ({
  GroupFormDialog: () => null,
}));
vi.mock("@/components/groups/ManageVaultsSheet", () => ({
  ManageVaultsSheet: ({
    group,
    open,
    onOpenChange,
    onSave,
  }: {
    group: Group | null;
    open: boolean;
    onOpenChange: (open: boolean) => void;
    onSave: (vaultAccess: VaultAccessItem[]) => Promise<void>;
  }) => open && group ? (
    <section aria-label={`vaults sheet ${group.id}`}>
      <span>{group.name}</span>
      <button onClick={() => onSave([{ vault_id: group.id, permission: "read" }])}>
        Save current vault access
      </button>
      <button onClick={() => onOpenChange(false)}>Close current vaults</button>
    </section>
  ) : null,
}));
vi.mock("@/components/groups/ManageMembersSheet", () => ({
  ManageMembersSheet: ({
    group,
    open,
    onOpenChange,
    onSave,
  }: {
    group: Group | null;
    open: boolean;
    onOpenChange: (open: boolean) => void;
    onSave: (userIds: number[]) => Promise<void>;
  }) => open && group ? (
    <section aria-label={`members sheet ${group.id}`}>
      <span>{group.name}</span>
      <button onClick={() => onSave([group.id])}>Save current members</button>
      <button onClick={() => onOpenChange(false)}>Close current members</button>
    </section>
  ) : null,
}));

function group(id: number): Group {
  return {
    id,
    name: id === 1 ? "Group A" : "Group B",
    description: null,
    created_at: "2024-01-01T00:00:00Z",
    org_id: null,
    organization_name: null,
  };
}

const queryClients = new Set<QueryClient>();
const pendingPromises = new Set<Promise<unknown>>();

function deferred<T>() {
  let resolve!: (value: T) => void;
  let settled = false;
  const promise = new Promise<T>((res) => {
    resolve = (value: T) => {
      if (settled) return;
      settled = true;
      res(value);
    };
  });
  pendingPromises.add(promise);
  return { promise, resolve };
}

describe("AdminGroupsPage issue #772 mutation ownership", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(async () => {
    cleanup();
    for (const promise of pendingPromises) {
      await Promise.resolve(promise);
    }
    await Promise.allSettled([...pendingPromises]);
    for (const queryClient of queryClients) {
      queryClient.clear();
      queryClient.unmount();
    }
    pendingPromises.clear();
    queryClients.clear();
    vi.restoreAllMocks();
  });

  it("keeps group B open and invalidates group A after A's pending save completes", async () => {
    const firstSave = deferred<unknown>();
    updateGroupMembersMock.mockReturnValueOnce(firstSave.promise);
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    queryClients.add(queryClient);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);

    try {
      render(
        <QueryClientProvider client={queryClient}>
          <AdminGroupsPage />
        </QueryClientProvider>,
      );

      fireEvent.click(screen.getByRole("button", { name: "Manage group A members" }));
      fireEvent.click(screen.getByRole("button", { name: "Save current members" }));
      await waitFor(() => expect(updateGroupMembersMock).toHaveBeenCalledWith(1, [1]));

      fireEvent.click(screen.getByRole("button", { name: "Close current members" }));
      fireEvent.click(screen.getByRole("button", { name: "Manage group B members" }));
      expect(screen.getByRole("region", { name: "members sheet 2" })).toBeInTheDocument();

      firstSave.resolve({ ok: true });
      await waitFor(() => {
        const invalidatedA = invalidate.mock.calls.some(
          ([options]) => JSON.stringify(options) === JSON.stringify({ queryKey: ["groups", 1, "members"] }),
        );
        expect(screen.queryByRole("region", { name: "members sheet 1" })).not.toBeInTheDocument();
        expect({
          bStillOpen: screen.queryByRole("region", { name: "members sheet 2" }) !== null,
          invalidatedA,
        }).toEqual({ bStillOpen: true, invalidatedA: true });
      });
    } finally {
      firstSave.resolve({ ok: true });
      await firstSave.promise;
    }
  });

  it("keeps group B open and invalidates group A after A's pending vault save completes", async () => {
    const firstSave = deferred<unknown>();
    updateGroupVaultsMock.mockReturnValueOnce(firstSave.promise);
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    queryClients.add(queryClient);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);

    try {
      render(
        <QueryClientProvider client={queryClient}>
          <AdminGroupsPage />
        </QueryClientProvider>,
      );

      fireEvent.click(screen.getByRole("button", { name: "Manage group A vaults" }));
      fireEvent.click(screen.getByRole("button", { name: "Save current vault access" }));
      await waitFor(() => {
        expect(updateGroupVaultsMock).toHaveBeenCalledWith(1, [
          { vault_id: 1, permission: "read" },
        ]);
      });

      fireEvent.click(screen.getByRole("button", { name: "Close current vaults" }));
      fireEvent.click(screen.getByRole("button", { name: "Manage group B vaults" }));
      expect(screen.getByRole("region", { name: "vaults sheet 2" })).toBeInTheDocument();

      firstSave.resolve({ ok: true });
      await waitFor(() => {
        const invalidatedA = invalidate.mock.calls.some(
          ([options]) => JSON.stringify(options) === JSON.stringify({ queryKey: ["groups", 1, "vaults"] }),
        );
        expect(screen.queryByRole("region", { name: "vaults sheet 1" })).not.toBeInTheDocument();
        expect({
          bStillOpen: screen.queryByRole("region", { name: "vaults sheet 2" }) !== null,
          invalidatedA,
        }).toEqual({ bStillOpen: true, invalidatedA: true });
      });
    } finally {
      firstSave.resolve({ ok: true });
      await firstSave.promise;
    }
  });

  it("keeps a reopened group A member editor open after its older save completes", async () => {
    const firstSave = deferred<unknown>();
    updateGroupMembersMock.mockReturnValueOnce(firstSave.promise);
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    queryClients.add(queryClient);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);

    try {
      render(
        <QueryClientProvider client={queryClient}>
          <AdminGroupsPage />
        </QueryClientProvider>,
      );

      fireEvent.click(screen.getByRole("button", { name: "Manage group A members" }));
      fireEvent.click(screen.getByRole("button", { name: "Save current members" }));
      await waitFor(() => expect(updateGroupMembersMock).toHaveBeenCalledWith(1, [1]));

      fireEvent.click(screen.getByRole("button", { name: "Close current members" }));
      fireEvent.click(screen.getByRole("button", { name: "Manage group A members" }));
      expect(screen.getByRole("region", { name: "members sheet 1" })).toBeInTheDocument();

      firstSave.resolve({ ok: true });
      await waitFor(() => {
        const invalidatedA = invalidate.mock.calls.some(
          ([options]) => JSON.stringify(options) === JSON.stringify({ queryKey: ["groups", 1, "members"] }),
        );
        expect(invalidatedA).toBe(true);
      });
      expect(screen.getByRole("region", { name: "members sheet 1" })).toBeInTheDocument();
    } finally {
      firstSave.resolve({ ok: true });
      await firstSave.promise;
    }
  });

  it("keeps a reopened group A vault editor open after its older save completes", async () => {
    const firstSave = deferred<unknown>();
    updateGroupVaultsMock.mockReturnValueOnce(firstSave.promise);
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    queryClients.add(queryClient);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);

    try {
      render(
        <QueryClientProvider client={queryClient}>
          <AdminGroupsPage />
        </QueryClientProvider>,
      );

      fireEvent.click(screen.getByRole("button", { name: "Manage group A vaults" }));
      fireEvent.click(screen.getByRole("button", { name: "Save current vault access" }));
      await waitFor(() => {
        expect(updateGroupVaultsMock).toHaveBeenCalledWith(1, [
          { vault_id: 1, permission: "read" },
        ]);
      });

      fireEvent.click(screen.getByRole("button", { name: "Close current vaults" }));
      fireEvent.click(screen.getByRole("button", { name: "Manage group A vaults" }));
      expect(screen.getByRole("region", { name: "vaults sheet 1" })).toBeInTheDocument();

      firstSave.resolve({ ok: true });
      await waitFor(() => {
        const invalidatedA = invalidate.mock.calls.some(
          ([options]) => JSON.stringify(options) === JSON.stringify({ queryKey: ["groups", 1, "vaults"] }),
        );
        expect(invalidatedA).toBe(true);
      });
      expect(screen.getByRole("region", { name: "vaults sheet 1" })).toBeInTheDocument();
    } finally {
      firstSave.resolve({ ok: true });
      await firstSave.promise;
    }
  });
});
