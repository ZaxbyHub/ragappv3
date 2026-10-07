import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Group } from "@/lib/api";
import type { GroupFormData } from "@/components/groups/GroupFormDialog";
import AdminGroupsPage from "./AdminGroupsPage";

const { updateGroupMock, deleteGroupMock, toastSuccessMock, toastErrorMock } = vi.hoisted(() => ({
  updateGroupMock: vi.fn(),
  deleteGroupMock: vi.fn(),
  toastSuccessMock: vi.fn(),
  toastErrorMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createGroup: vi.fn(),
  updateGroup: (...args: unknown[]) => updateGroupMock(...args),
  deleteGroup: (...args: unknown[]) => deleteGroupMock(...args),
  updateGroupMembers: vi.fn(),
  updateGroupVaults: vi.fn(),
}));

vi.mock("@/components/auth/RoleGuard", () => ({
  AdminGuard: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: { children: ReactNode }) => <button {...props}>{children}</button>,
}));
vi.mock("@/components/layout/PageTitleHeader", () => ({
  PageTitleHeader: ({
    title,
    description,
    actions,
    before,
    srOnly,
    id,
  }: {
    title: ReactNode;
    description?: ReactNode;
    actions?: ReactNode;
    before?: ReactNode;
    srOnly?: boolean;
    id?: string;
  }) =>
    srOnly ? (
      <h1 id={id} className="sr-only">{title}</h1>
    ) : (
      <div>
        {before}
        <div>
          <h1 id={id}>{title}</h1>
          {description ?? null}
        </div>
        {actions ?? null}
      </div>
    ),
}));
vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children, open }: { children: ReactNode; open: boolean }) =>
    open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
}));
vi.mock("lucide-react", () => ({ Plus: () => <span /> }));
vi.mock("sonner", () => ({ toast: { success: toastSuccessMock, error: toastErrorMock } }));

vi.mock("@/components/groups/GroupTable", () => ({
  GroupTable: ({
    onEdit,
    onDelete,
  }: {
    onEdit: (group: Group) => void;
    onDelete: (group: Group) => void;
  }) => (
    <div>
      <button onClick={() => onEdit(group(1))}>Edit group A</button>
      <button onClick={() => onDelete(group(1))}>Delete group A</button>
    </div>
  ),
}));
vi.mock("@/components/groups/GroupFormDialog", () => ({
  GroupFormDialog: ({
    mode,
    open,
    onOpenChange,
    onSubmit,
  }: {
    mode: "create" | "edit";
    open: boolean;
    onOpenChange: (open: boolean) => void;
    onSubmit: (data: GroupFormData) => Promise<void>;
  }) =>
    open && mode === "edit" ? (
      <section aria-label="edit group dialog">
        <button
          onClick={() =>
            void onSubmit({ name: "Group A updated", description: null, org_id: 1 })
          }
        >
          Submit group edit
        </button>
        <button onClick={() => onOpenChange(false)}>Close group edit</button>
      </section>
    ) : null,
}));
vi.mock("@/components/groups/ManageMembersSheet", () => ({
  ManageMembersSheet: () => null,
}));
vi.mock("@/components/groups/ManageVaultsSheet", () => ({
  ManageVaultsSheet: () => null,
}));

function group(id: number): Group {
  return {
    id,
    name: "Group A",
    description: null,
    created_at: "2024-01-01T00:00:00Z",
    org_id: 1,
    organization_name: "Acme",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("AdminGroupsPage issue #772 callback recurrence", () => {
  const queryClients: QueryClient[] = [];

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
    for (const queryClient of queryClients.splice(0)) queryClient.clear();
    vi.restoreAllMocks();
  });

  it("keeps a reopened edit dialog open after an older update succeeds", async () => {
    const firstUpdate = deferred<Group>();
    updateGroupMock.mockReturnValueOnce(firstUpdate.promise);
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    queryClients.push(queryClient);

    try {
      render(
        <QueryClientProvider client={queryClient}>
          <AdminGroupsPage />
        </QueryClientProvider>,
      );
      fireEvent.click(screen.getByRole("button", { name: "Edit group A" }));
      fireEvent.click(screen.getByRole("button", { name: "Submit group edit" }));
      await waitFor(() => expect(updateGroupMock).toHaveBeenCalledWith(1, "Group A updated", null));

      fireEvent.click(screen.getByRole("button", { name: "Close group edit" }));
      fireEvent.click(screen.getByRole("button", { name: "Edit group A" }));
      expect(screen.getByRole("region", { name: "edit group dialog" })).toBeInTheDocument();

      await act(async () => {
        firstUpdate.resolve(group(1));
        await waitFor(() =>
          expect(toastSuccessMock).toHaveBeenCalledWith("Group updated successfully"),
        );
      });
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getByRole("region", { name: "edit group dialog" })).toBeInTheDocument();
    } finally {
      await act(async () => {
        firstUpdate.resolve(group(1));
        await Promise.resolve();
      });
    }
  });

  it("keeps a reopened delete dialog open after an older delete succeeds", async () => {
    const firstDelete = deferred<void>();
    deleteGroupMock.mockReturnValueOnce(firstDelete.promise);
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    queryClients.push(queryClient);

    try {
      render(
        <QueryClientProvider client={queryClient}>
          <AdminGroupsPage />
        </QueryClientProvider>,
      );
      fireEvent.click(screen.getByRole("button", { name: "Delete group A" }));
      fireEvent.click(screen.getByRole("button", { name: "Confirm delete group" }));
      await waitFor(() => expect(deleteGroupMock).toHaveBeenCalledWith(1));

      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      fireEvent.click(screen.getByRole("button", { name: "Delete group A" }));
      expect(screen.getByRole("button", { name: "Confirm delete group" })).toBeInTheDocument();

      await act(async () => {
        firstDelete.resolve();
        await waitFor(() =>
          expect(toastSuccessMock).toHaveBeenCalledWith("Group deleted successfully"),
        );
      });
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getByRole("button", { name: "Confirm delete group" })).toBeInTheDocument();
    } finally {
      await act(async () => {
        firstDelete.resolve();
        await Promise.resolve();
      });
    }
  });
});
