import type { ButtonHTMLAttributes, ReactNode } from "react";

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { captureAuthOwner, reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";
import { setJwtAccessToken } from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";
import AdminGroupsPage from "@/pages/AdminGroupsPage";

type GroupRecord = {
  id: number;
  name: string;
  description: string;
};

type FormDataRecord = {
  name: string;
  description: string;
  org_id: number;
};

type GroupFormProps = {
  group?: GroupRecord | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (data: FormDataRecord) => void | Promise<void>;
  isLoading?: boolean;
};

const fixtures = vi.hoisted(() => ({
  groupA: { id: 1, name: "Alpha", description: "A" },
  groupB: { id: 2, name: "Beta", description: "B" },
  userA: { id: 11, username: "alice", full_name: "Alice", role: "admin" as const, is_active: true },
  userB: { id: 12, username: "bob", full_name: "Bob", role: "admin" as const, is_active: true },
  updateGroup: vi.fn(),
  createGroup: vi.fn(),
  deleteGroup: vi.fn(),
  updateGroupMembers: vi.fn(),
  updateGroupVaults: vi.fn(),
}));

const seams = vi.hoisted(() => ({
  formProps: new Map<string, unknown>(),
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("@/lib/api", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/api")>();
  return {
    ...actual,
    updateGroup: fixtures.updateGroup,
    createGroup: fixtures.createGroup,
    deleteGroup: fixtures.deleteGroup,
    updateGroupMembers: fixtures.updateGroupMembers,
    updateGroupVaults: fixtures.updateGroupVaults,
  };
});

vi.mock("@/components/auth/RoleGuard", () => ({
  AdminGuard: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button {...props}>{children}</button>
  ),
}));

vi.mock("@/components/layout/PageTitleHeader", () => ({
  PageTitleHeader: ({ children }: { children?: ReactNode }) => <>{children}</>,
}));

vi.mock("@/components/groups/ManageMembersSheet", () => ({
  ManageMembersSheet: () => null,
}));

vi.mock("@/components/groups/ManageVaultsSheet", () => ({
  ManageVaultsSheet: () => null,
}));

vi.mock("@/components/groups/GroupTable", () => {
  const MockGroupTable = ({
    onEdit,
  }: {
    onEdit: (group: GroupRecord) => void;
  }) => (
    <div data-testid="group-table">
      {[fixtures.groupA, fixtures.groupB].map((group) => (
        <button key={group.id} type="button" onClick={() => onEdit(group)}>
          Edit {group.name}
        </button>
      ))}
    </div>
  );
  return { GroupTable: MockGroupTable, default: MockGroupTable };
});

vi.mock("@/components/groups/GroupFormDialog", () => {
  const MockGroupFormDialog = (props: GroupFormProps) => {
    if (props.group) {
      seams.formProps.set(String(props.group.id), props);
    }
    if (!props.open) return null;
    return (
      <div role="dialog" aria-label={`Edit ${props.group?.name ?? "group"}`}>
        <button type="button" onClick={() => props.onOpenChange(false)}>
          Cancel
        </button>
        <button
          type="button"
          disabled={props.isLoading}
          onClick={() => {
            void props.onSubmit({
              name: props.group?.name ?? "",
              description: props.group?.description ?? "",
              org_id: 7,
            });
          }}
        >
          Save
        </button>
      </div>
    );
  };
  return { GroupFormDialog: MockGroupFormDialog, default: MockGroupFormDialog };
});

vi.mock("lucide-react", () => ({ Plus: () => null }));
vi.mock("sonner", () => ({ toast: { success: seams.toastSuccess, error: seams.toastError } }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

function formProps(groupId: number): GroupFormProps {
  const props = seams.formProps.get(String(groupId));
  if (!props) throw new Error(`missing form props for group ${groupId}`);
  return props as GroupFormProps;
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <AdminGroupsPage />
      </QueryClientProvider>,
    ),
  };
}

function resetAuth() {
  setJwtAccessToken(null);
  reserveReplacementAuthOwner();
  useAuthStore.setState({ user: null });
}

beforeEach(() => {
  seams.formProps.clear();
  seams.toastSuccess.mockReset();
  seams.toastError.mockReset();
  fixtures.updateGroup.mockReset();
  fixtures.createGroup.mockReset();
  fixtures.deleteGroup.mockReset();
  fixtures.updateGroupMembers.mockReset();
  fixtures.updateGroupVaults.mockReset();
  fixtures.updateGroup.mockResolvedValue(fixtures.groupA);
  resetAuth();
});

afterEach(() => {
  cleanup();
  resetAuth();
});

describe("AdminGroupsPage ownership guardrails", () => {
  it("keeps B pending when A completes late, then accepts the current B completion", async () => {
    const saveA = deferred<GroupRecord>();
    const saveB = deferred<GroupRecord>();
    fixtures.updateGroup.mockImplementation(() =>
      fixtures.updateGroup.mock.calls.length === 1 ? saveA.promise : saveB.promise,
    );
    renderPage();
    await screen.findByRole("button", { name: "Edit Alpha" });

    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha" }));
    const propsA = formProps(fixtures.groupA.id);
    await act(async () => {
      void propsA.onSubmit({ name: "Alpha", description: "A", org_id: 7 });
      await Promise.resolve();
    });
    await waitFor(() => expect(fixtures.updateGroup).toHaveBeenCalledTimes(1));

    act(() => {
      setJwtAccessToken("jwt-b");
      useAuthStore.setState({ user: fixtures.userB });
      reserveReplacementAuthOwner();
    });
    fireEvent.click(screen.getByRole("button", { name: "Edit Beta" }));
    const propsB = formProps(fixtures.groupB.id);
    await act(async () => {
      void propsB.onSubmit({ name: "Beta", description: "B", org_id: 7 });
      await Promise.resolve();
    });
    await waitFor(() => expect(fixtures.updateGroup).toHaveBeenCalledTimes(2));

    await act(async () => {
      saveA.resolve(fixtures.groupA);
      await Promise.resolve();
    });
    expect(formProps(fixtures.groupB.id).open).toBe(true);
    expect(formProps(fixtures.groupB.id).isLoading).toBe(true);
    expect(seams.toastSuccess).not.toHaveBeenCalled();

    await act(async () => {
      saveB.resolve(fixtures.groupB);
      await Promise.resolve();
    });
    await waitFor(() => expect(seams.toastSuccess).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog", { name: "Edit Beta" })).not.toBeInTheDocument();
  });

  it("stops outer publication after toast owner replacement, then accepts a new B action", async () => {
    fixtures.updateGroup.mockResolvedValue(fixtures.groupA);
    const { queryClient } = renderPage();
    const outerInvalidation = vi.spyOn(queryClient, "invalidateQueries");
    await screen.findByRole("button", { name: "Edit Alpha" });
    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha" }));
    const propsA = formProps(fixtures.groupA.id);
    seams.toastSuccess.mockImplementationOnce(() => {
      reserveReplacementAuthOwner();
    });

    await act(async () => {
      void propsA.onSubmit({ name: "Alpha", description: "A", org_id: 7 });
      await Promise.resolve();
    });
    await waitFor(() => expect(fixtures.updateGroup).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(seams.toastSuccess).toHaveBeenCalledTimes(1));
    expect(outerInvalidation).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Edit Beta" }));
    const propsB = formProps(fixtures.groupB.id);
    await act(async () => {
      void propsB.onSubmit({ name: "Beta", description: "B", org_id: 7 });
      await Promise.resolve();
    });
    await waitFor(() => expect(fixtures.updateGroup).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(seams.toastSuccess).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(outerInvalidation).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog", { name: "Edit Beta" })).not.toBeInTheDocument();
  });

  it("rejects retained A callbacks and late A finalizers after a same-group reopen", async () => {
    const saveA = deferred<GroupRecord>();
    const saveB = deferred<GroupRecord>();
    fixtures.updateGroup.mockImplementation(() =>
      fixtures.updateGroup.mock.calls.length === 1 ? saveA.promise : saveB.promise,
    );
    renderPage();
    await screen.findByRole("button", { name: "Edit Alpha" });
    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha" }));
    const propsA = formProps(fixtures.groupA.id);

    await act(async () => {
      void propsA.onSubmit({ name: "Alpha", description: "A", org_id: 7 });
      await Promise.resolve();
    });
    await waitFor(() => expect(fixtures.updateGroup).toHaveBeenCalledTimes(1));

    act(() => propsA.onOpenChange(false));
    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha" }));
    const propsB = formProps(fixtures.groupA.id);
    expect(propsB.open).toBe(true);

    await act(async () => {
      propsA.onOpenChange(false);
      void propsA.onSubmit({ name: "Alpha", description: "A", org_id: 7 });
      await Promise.resolve();
    });
    expect(formProps(fixtures.groupA.id).open).toBe(true);
    expect(fixtures.updateGroup).toHaveBeenCalledTimes(1);

    await act(async () => {
      void propsB.onSubmit({ name: "Alpha", description: "A", org_id: 7 });
      await Promise.resolve();
    });
    await waitFor(() => expect(fixtures.updateGroup).toHaveBeenCalledTimes(2));

    await act(async () => {
      saveA.resolve(fixtures.groupA);
      await Promise.resolve();
    });
    expect(formProps(fixtures.groupA.id).open).toBe(true);
    expect(formProps(fixtures.groupA.id).isLoading).toBe(true);
    expect(seams.toastSuccess).toHaveBeenCalledTimes(1);

    await act(async () => {
      saveB.resolve(fixtures.groupA);
      await Promise.resolve();
    });
    await waitFor(() => expect(seams.toastSuccess).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog", { name: "Edit Alpha" })).not.toBeInTheDocument();
  });

  it("retires a retained dialog callback on synchronous same-owner principal replacement", async () => {
    setJwtAccessToken("jwt-principal");
    useAuthStore.setState({ user: fixtures.userA });
    renderPage();
    await screen.findByRole("button", { name: "Edit Alpha" });
    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha" }));
    const propsA = formProps(fixtures.groupA.id);
    const ownerA = captureAuthOwner();

    act(() => {
      useAuthStore.setState({ user: fixtures.userB });
      // Invoke retained callbacks before React commits the new scope.
      propsA.onOpenChange(false);
      void propsA.onSubmit({ name: "Alpha", description: "A", org_id: 7 });
    });

    expect(captureAuthOwner()).toBe(ownerA);
    expect(screen.queryByRole("dialog", { name: "Edit Alpha" })).not.toBeInTheDocument();
    expect(fixtures.updateGroup).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Edit Beta" }));
    const propsB = formProps(fixtures.groupB.id);
    await act(async () => {
      void propsB.onSubmit({ name: "Beta", description: "B", org_id: 7 });
      await Promise.resolve();
    });
    await waitFor(() => expect(fixtures.updateGroup).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(seams.toastSuccess).toHaveBeenCalledTimes(1));
  });
  it("keeps a retired session failure silent while the current B save remains pending", async () => {
    const saveA = deferred<GroupRecord>();
    const saveB = deferred<GroupRecord>();
    const outcomes: Array<Promise<unknown>> = [];
    fixtures.updateGroup.mockImplementation(() =>
      fixtures.updateGroup.mock.calls.length === 1 ? saveA.promise : saveB.promise,
    );
    try {
      renderPage();
      fireEvent.click(screen.getByRole("button", { name: "Edit Alpha" }));
      const propsA = formProps(fixtures.groupA.id);
      await act(async () => {
        outcomes.push(Promise.resolve(propsA.onSubmit({ name: "Alpha", description: "A", org_id: 7 }))
          .then(() => null, (error: unknown) => error));
        await Promise.resolve();
      });
      await waitFor(() => expect(fixtures.updateGroup).toHaveBeenCalledTimes(1));
      act(() => reserveReplacementAuthOwner());
      fireEvent.click(screen.getByRole("button", { name: "Edit Beta" }));
      const propsB = formProps(fixtures.groupB.id);
      await act(async () => {
        outcomes.push(Promise.resolve(propsB.onSubmit({ name: "Beta", description: "B", org_id: 7 }))
          .then(() => null, (error: unknown) => error));
        await Promise.resolve();
      });
      await waitFor(() => expect(fixtures.updateGroup).toHaveBeenCalledTimes(2));
      await act(async () => {
        saveA.reject(new Error("Retired A failed"));
        expect(await outcomes[0]).toBeInstanceOf(Error);
      });
      expect(seams.toastError).not.toHaveBeenCalled();
      expect(formProps(fixtures.groupB.id).isLoading).toBe(true);
      expect(screen.getByRole("dialog", { name: "Edit Beta" })).toBeInTheDocument();
      await act(async () => {
        saveB.resolve(fixtures.groupB);
        expect(await outcomes[1]).toBeNull();
      });
      expect(seams.toastSuccess).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole("dialog", { name: "Edit Beta" })).not.toBeInTheDocument();
    } finally {
      await act(async () => {
        saveA.resolve(fixtures.groupA);
        saveB.resolve(fixtures.groupB);
        await Promise.all(outcomes);
      });
    }
  });

});
