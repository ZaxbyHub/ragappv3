import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Group, VaultAccessItem } from "@/lib/api";
import AdminGroupsPage from "./AdminGroupsPage";

const {
  updateGroupMembersMock,
  updateGroupVaultsMock,
  toastErrorMock,
  toastSuccessMock,
} = vi.hoisted(() => ({
  updateGroupMembersMock: vi.fn(),
  updateGroupVaultsMock: vi.fn(),
  toastErrorMock: vi.fn(),
  toastSuccessMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createGroup: vi.fn(),
  updateGroup: vi.fn(),
  deleteGroup: vi.fn(),
  updateGroupMembers: (...args: unknown[]) => updateGroupMembersMock(...args),
  updateGroupVaults: (...args: unknown[]) => updateGroupVaultsMock(...args),
}));
vi.mock("@/components/auth/RoleGuard", () => ({
  AdminGuard: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: { children: ReactNode }) => (
    <button {...props}>{children}</button>
  ),
}));
vi.mock("@/components/layout/PageTitleHeader", () => ({
  PageTitleHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));
vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children, open }: { children: ReactNode; open: boolean }) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
}));
vi.mock("lucide-react", () => ({ Plus: () => <span /> }));
vi.mock("sonner", () => ({ toast: { error: toastErrorMock, success: toastSuccessMock } }));

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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const activeQueryClients: QueryClient[] = [];

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { gcTime: 0, retry: false } },
  });
  activeQueryClients.push(queryClient);
  const view = render(
    <QueryClientProvider client={queryClient}>
      <AdminGroupsPage />
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

vi.mock("@/components/groups/GroupTable", () => ({
  GroupTable: ({
    onManageMembers,
    onManageVaults,
  }: {
    onManageMembers: (selected: Group) => void;
    onManageVaults: (selected: Group) => void;
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
    group: selected,
    open,
    editorToken,
    onOpenChange,
    onSave,
  }: {
    group: Group | null;
    open: boolean;
    editorToken?: number;
    onOpenChange: (open: boolean) => void;
    onSave: (vaultAccess: VaultAccessItem[]) => Promise<void>;
  }) => {
    if (!open || !selected) return null;
    return (
      <section aria-label={`vaults sheet ${selected.id}`}>
        <output data-testid="vaults-editor-token">{String(editorToken)}</output>
        <button
          onClick={() => void onSave([{ vault_id: 101, permission: "read" }]).catch(() => undefined)}
        >
          Save current vaults
        </button>
        <button onClick={() => onOpenChange(false)}>Close current vaults</button>
      </section>
    );
  },
}));
vi.mock("@/components/groups/ManageMembersSheet", () => ({
  ManageMembersSheet: ({
    group: selected,
    open,
    editorToken,
    onOpenChange,
    onSave,
  }: {
    group: Group | null;
    open: boolean;
    editorToken?: number;
    onOpenChange: (open: boolean) => void;
    onSave: (userIds: number[]) => Promise<void>;
  }) => {
    if (!open || !selected) return null;
    return (
      <section aria-label={`members sheet ${selected.id}`}>
        <output data-testid="members-editor-token">{String(editorToken)}</output>
        <button onClick={() => void onSave([selected.id]).catch(() => undefined)}>
          Save current members
        </button>
        <button onClick={() => onOpenChange(false)}>Close current members</button>
      </section>
    );
  },
}));

describe("AdminGroupsPage issue #772 page ownership seams", () => {
  beforeEach(() => {
    updateGroupMembersMock.mockReset().mockResolvedValue(undefined);
    updateGroupVaultsMock.mockReset().mockResolvedValue(undefined);
    toastErrorMock.mockReset();
    toastSuccessMock.mockReset();
  });

  afterEach(() => {
    cleanup();
    activeQueryClients.splice(0).forEach((queryClient) => queryClient.clear());
  });

  it("observes a new editorToken on group switch and same-group reopen", async () => {
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Manage group A members" }));
    const firstToken = Number(screen.getByTestId("members-editor-token").textContent);
    fireEvent.click(screen.getByRole("button", { name: "Manage group B members" }));
    const secondToken = Number(screen.getByTestId("members-editor-token").textContent);
    expect(secondToken).toBeGreaterThan(firstToken);

    fireEvent.click(screen.getByRole("button", { name: "Close current members" }));
    fireEvent.click(screen.getByRole("button", { name: "Manage group B members" }));
    const reopenedToken = Number(screen.getByTestId("members-editor-token").textContent);
    expect(reopenedToken).toBeGreaterThan(secondToken);
  });

  it("keeps the reopened member editor open when the older save rejects", async () => {
    const oldSave = deferred<void>();
    updateGroupMembersMock.mockReturnValueOnce(oldSave.promise);
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Manage group A members" }));
    fireEvent.click(screen.getByRole("button", { name: "Save current members" }));
    await waitFor(() => expect(updateGroupMembersMock).toHaveBeenCalledWith(1, [1]));
    const oldToken = Number(screen.getByTestId("members-editor-token").textContent);

    fireEvent.click(screen.getByRole("button", { name: "Close current members" }));
    fireEvent.click(screen.getByRole("button", { name: "Manage group A members" }));
    expect(Number(screen.getByTestId("members-editor-token").textContent)).toBeGreaterThan(oldToken);

    oldSave.reject(new Error("older save failed"));
    await waitFor(() => expect(toastErrorMock).toHaveBeenCalledWith("older save failed"));
    expect(screen.getByRole("region", { name: "members sheet 1" })).toBeInTheDocument();
  });

  it("observes a new vault editorToken on group switch and same-group reopen", () => {
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Manage group A vaults" }));
    const firstToken = Number(screen.getByTestId("vaults-editor-token").textContent);
    fireEvent.click(screen.getByRole("button", { name: "Manage group B vaults" }));
    const secondToken = Number(screen.getByTestId("vaults-editor-token").textContent);
    expect(secondToken).toBeGreaterThan(firstToken);

    fireEvent.click(screen.getByRole("button", { name: "Close current vaults" }));
    fireEvent.click(screen.getByRole("button", { name: "Manage group B vaults" }));
    const reopenedToken = Number(screen.getByTestId("vaults-editor-token").textContent);
    expect(reopenedToken).toBeGreaterThan(secondToken);
  });

  it("keeps the reopened vault editor open when the older save rejects", async () => {
    const oldSave = deferred<void>();
    updateGroupVaultsMock.mockReturnValueOnce(oldSave.promise);
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Manage group A vaults" }));
    fireEvent.click(screen.getByRole("button", { name: "Save current vaults" }));
    await waitFor(() => expect(updateGroupVaultsMock).toHaveBeenCalledWith(1, [{ vault_id: 101, permission: "read" }]));
    const oldToken = Number(screen.getByTestId("vaults-editor-token").textContent);

    fireEvent.click(screen.getByRole("button", { name: "Close current vaults" }));
    fireEvent.click(screen.getByRole("button", { name: "Manage group A vaults" }));
    expect(Number(screen.getByTestId("vaults-editor-token").textContent)).toBeGreaterThan(oldToken);

    oldSave.reject(new Error("older vault save failed"));
    await waitFor(() => expect(toastErrorMock).toHaveBeenCalledWith("older vault save failed"));
    expect(screen.getByRole("region", { name: "vaults sheet 1" })).toBeInTheDocument();
  });

});
