import {
  Profiler,
  type InputHTMLAttributes,
  type ProfilerOnRenderCallback,
  type ReactNode,
  useState,
} from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider, onlineManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManageMembersSheet } from "./ManageMembersSheet";
import { ManageVaultsSheet } from "./ManageVaultsSheet";
import type { Group, GroupMember, Vault, VaultAccessItem } from "@/lib/api";

const { eligibleMembersMock, groupMembersMock, listVaultsMock, getGroupVaultsMock } =
  vi.hoisted(() => ({
    eligibleMembersMock: vi.fn(),
    groupMembersMock: vi.fn(),
    listVaultsMock: vi.fn(),
    getGroupVaultsMock: vi.fn(),
  }));

vi.mock("@/lib/api", () => ({
  getEligibleGroupMembers: (...args: unknown[]) => eligibleMembersMock(...args),
  getGroupMembers: (...args: unknown[]) => groupMembersMock(...args),
  listVaults: (...args: unknown[]) => listVaultsMock(...args),
  getGroupVaults: (...args: unknown[]) => getGroupVaultsMock(...args),
}));

// Keep the production Radix Sheet real in this file. The other controls are
// mocked only to make selection and permission transitions deterministic in
// jsdom; the dismissal tests therefore exercise X, Escape, and outside clicks.
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: { children: ReactNode }) => (
    <button {...props}>{children}</button>
  ),
}));
vi.mock("@/components/ui/input", () => ({
  Input: (props: InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
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
  Select: ({
    children,
    disabled,
    onValueChange,
    value,
  }: {
    children: ReactNode;
    disabled?: boolean;
    onValueChange?: (value: string) => void;
    value?: string;
  }) => (
    <button
      type="button"
      data-testid="permission-select"
      data-value={value}
      disabled={disabled}
      onClick={() => onValueChange?.("write")}
    >
      {children}
    </button>
  ),
  SelectContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SelectItem: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  SelectTrigger: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  SelectValue: () => <span />,
}));
vi.mock("lucide-react", () => ({
  FolderOpen: () => <span />,
  Loader2: () => <span />,
  Search: () => <span />,
  Shield: () => <span />,
  Users: () => <span />,
  X: () => <span />,
}));

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

function makeQueryClient() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { gcTime: 0, retry: false } },
  });
  activeQueryClients.push(queryClient);
  return queryClient;
}

function makeGroup(id = 7): Group {
  return {
    id,
    name: `Group ${id}`,
    description: null,
    created_at: "2024-01-01T00:00:00Z",
    org_id: 1,
    organization_name: "Acme",
  };
}

function makeMember(id = 42): GroupMember {
  return { id, username: "alice", full_name: "Alice Example" };
}

function makeVault(id = 101): Vault {
  return {
    id,
    name: "Knowledge Vault",
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

type DismissalPath = "close-button" | "escape" | "outside";

async function triggerDismissal(path: DismissalPath) {
  if (path === "close-button") {
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
  } else if (path === "escape") {
    fireEvent.keyDown(document, { key: "Escape", code: "Escape", bubbles: true });
  } else {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    fireEvent.pointerDown(document.body, {
      button: 0,
      pointerType: "mouse",
      clientX: 0,
      clientY: 0,
    });
    fireEvent.click(document.body, {
      button: 0,
      detail: 1,
      clientX: 0,
      clientY: 0,
    });
  }
}

function renderMembers(
  onSave: (userIds: number[]) => Promise<void>,
  options: {
    group?: Group;
    editorToken?: number;
    onOpenChange?: (open: boolean) => void;
    profiler?: ProfilerOnRenderCallback;
  } = {},
) {
  const queryClient = makeQueryClient();
  const sheet = (
    <ManageMembersSheet
      group={options.group ?? makeGroup()}
      open
      editorToken={options.editorToken ?? 1}
      onOpenChange={options.onOpenChange ?? vi.fn()}
      onSave={onSave}
    />
  );
  const view = render(
    <QueryClientProvider client={queryClient}>
      {options.profiler ? (
        <Profiler id="members-sheet" onRender={options.profiler}>
          {sheet}
        </Profiler>
      ) : (
        sheet
      )}
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

function renderVaults(
  onSave: (vaultAccess: VaultAccessItem[]) => Promise<void>,
  options: {
    group?: Group;
    editorToken?: number;
    onOpenChange?: (open: boolean) => void;
    profiler?: ProfilerOnRenderCallback;
  } = {},
) {
  const queryClient = makeQueryClient();
  const sheet = (
    <ManageVaultsSheet
      group={options.group ?? makeGroup()}
      open
      editorToken={options.editorToken ?? 1}
      onOpenChange={options.onOpenChange ?? vi.fn()}
      onSave={onSave}
    />
  );
  const view = render(
    <QueryClientProvider client={queryClient}>
      {options.profiler ? (
        <Profiler id="vaults-sheet" onRender={options.profiler}>
          {sheet}
        </Profiler>
      ) : (
        sheet
      )}
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

describe("issue #772 group editor regression coverage", () => {
  beforeEach(() => {
    onlineManager.setOnline(true);
    eligibleMembersMock.mockReset().mockResolvedValue([makeMember()]);
    groupMembersMock.mockReset().mockResolvedValue([]);
    listVaultsMock.mockReset().mockResolvedValue({ vaults: [makeVault()] });
    getGroupVaultsMock.mockReset().mockResolvedValue([]);
  });

  afterEach(() => {
    onlineManager.setOnline(true);
    cleanup();
    activeQueryClients.splice(0).forEach((queryClient) => queryClient.clear());
  });

  it("saves a selected member and resets a populated group A opening to empty group B", async () => {
    groupMembersMock.mockImplementation((groupId: number) =>
      Promise.resolve(groupId === 7 ? [makeMember()] : []),
    );
    const onSave = vi.fn<(userIds: number[]) => Promise<void>>().mockResolvedValue(undefined);
    const view = renderMembers(onSave, { group: makeGroup(7), editorToken: 1 });

    const member = await screen.findByLabelText("Select alice");
    await waitFor(() => expect(member).toBeChecked());
    const save = screen.getByRole("button", { name: "Save member changes" });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);
    await waitFor(() => expect(onSave).toHaveBeenCalledWith([42]));
    await waitFor(() => expect(save).not.toBeDisabled());

    view.rerender(
      <QueryClientProvider client={view.queryClient}>
        <ManageMembersSheet
          group={makeGroup(8)}
          open
          editorToken={2}
          onOpenChange={vi.fn()}
          onSave={onSave}
        />
      </QueryClientProvider>,
    );
    const reopenedMember = await screen.findByLabelText("Select alice");
    await waitFor(() => expect(reopenedMember).not.toBeChecked());
    const reopenedSave = screen.getByRole("button", { name: "Save member changes" });
    await waitFor(() => expect(reopenedSave).not.toBeDisabled());
    fireEvent.click(reopenedSave);
    await waitFor(() => expect(onSave).toHaveBeenLastCalledWith([]));
  });

  it("saves a positive write permission payload", async () => {
    getGroupVaultsMock.mockResolvedValue([{ id: 101, name: "Knowledge Vault", org_id: 1, permission: "read" }]);
    const onSave = vi.fn<(access: VaultAccessItem[]) => Promise<void>>().mockResolvedValue(undefined);
    renderVaults(onSave);

    const checkbox = await screen.findByLabelText("Grant access to Knowledge Vault");
    await waitFor(() => expect(checkbox).toBeChecked());
    await waitFor(() => expect(screen.getByRole("button", { name: "Save vault access changes" })).not.toBeDisabled());
    fireEvent.click(screen.getByTestId("permission-select"));
    fireEvent.click(screen.getByRole("button", { name: "Save vault access changes" }));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith([{ vault_id: 101, permission: "write" }]),
    );
  });

  it("keeps a member edit visible and retryable after a post-init refetch error", async () => {
    const onSave = vi.fn<(userIds: number[]) => Promise<void>>().mockResolvedValue(undefined);
    const view = renderMembers(onSave);
    const member = await screen.findByLabelText("Select alice");
    fireEvent.click(member);
    expect(member).toBeChecked();

    groupMembersMock.mockRejectedValueOnce(new Error("temporary refresh failure"));
    const refetch = view.queryClient.refetchQueries({ queryKey: ["groups", 7, "members"] });
    await waitFor(() => expect(view.queryClient.getQueryState(["groups", 7, "members"])?.status).toBe("error"));
    await refetch;

    expect(screen.getByLabelText("Select alice")).toBeChecked();
    await screen.findByText(/The latest member refresh failed\. Showing the last loaded members\./i);
    const save = screen.getByRole("button", { name: "Save member changes" });
    expect(save).not.toBeDisabled();

    fireEvent.click(save);
    await waitFor(() => expect(onSave).toHaveBeenCalledWith([42]));
    await waitFor(() => expect(save).not.toBeDisabled());

    groupMembersMock.mockResolvedValueOnce([]);
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(groupMembersMock).toHaveBeenCalledTimes(3));
    expect(screen.getByLabelText("Select alice")).toBeChecked();
    await waitFor(() =>
      expect(screen.queryByText(/The latest member refresh failed\. Showing the last loaded members\./i)).not.toBeInTheDocument(),
    );
  });

  it("keeps a vault edit visible and retryable after a post-init refetch error", async () => {
    const onSave = vi.fn<(access: VaultAccessItem[]) => Promise<void>>().mockResolvedValue(undefined);
    const view = renderVaults(onSave);
    const checkbox = await screen.findByLabelText("Grant access to Knowledge Vault");
    fireEvent.click(checkbox);
    expect(checkbox).toBeChecked();

    getGroupVaultsMock.mockRejectedValueOnce(new Error("temporary refresh failure"));
    const refetch = view.queryClient.refetchQueries({ queryKey: ["groups", 7, "vaults"] });
    await waitFor(() => expect(view.queryClient.getQueryState(["groups", 7, "vaults"])?.status).toBe("error"));
    await refetch;

    expect(screen.getByLabelText("Grant access to Knowledge Vault")).toBeChecked();
    await screen.findByText(/The latest vault-access refresh failed\. Showing the last loaded access\./i);
    const save = screen.getByRole("button", { name: "Save vault access changes" });
    expect(save).not.toBeDisabled();

    fireEvent.click(save);
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith([{ vault_id: 101, permission: "read" }]),
    );
    await waitFor(() => expect(save).not.toBeDisabled());

    getGroupVaultsMock.mockResolvedValueOnce([]);
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(getGroupVaultsMock).toHaveBeenCalledTimes(3));
    expect(screen.getByLabelText("Grant access to Knowledge Vault")).toBeChecked();
    await waitFor(() =>
      expect(screen.queryByText(/The latest vault-access refresh failed\. Showing the last loaded access\./i)).not.toBeInTheDocument(),
    );
  });

  it("fails closed for an initial member read error", async () => {
    groupMembersMock.mockRejectedValueOnce(new Error("initial read failure"));
    const view = renderMembers(vi.fn().mockResolvedValue(undefined));
    await waitFor(() => expect(view.queryClient.getQueryState(["groups", 7, "members"])?.status).toBe("error"));
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(/unable to load/i);
  });

  it("fails closed for an initial vault catalog read error", async () => {
    listVaultsMock.mockRejectedValueOnce(new Error("initial read failure"));
    const view = renderVaults(vi.fn().mockResolvedValue(undefined));
    await waitFor(() => expect(view.queryClient.getQueryState(["vaults"])?.status).toBe("error"));
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(/unable to load/i);
  });

  it("shows an offline empty-read message and keeps member Save disabled", async () => {
    onlineManager.setOnline(false);
    const view = renderMembers(vi.fn().mockResolvedValue(undefined));
    await waitFor(() => expect(view.queryClient.getQueryState(["groups", 7, "eligible-members"])?.fetchStatus).toBe("paused"));
    expect(screen.getByRole("status")).toHaveTextContent(/offline|reconnect/i);
    expect(screen.queryByText(/no org members available/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
    view.queryClient.clear();
  });

  it("shows an offline empty-read message and keeps vault Save disabled", async () => {
    onlineManager.setOnline(false);
    const view = renderVaults(vi.fn().mockResolvedValue(undefined));
    await waitFor(() => expect(view.queryClient.getQueryState(["vaults"])?.fetchStatus).toBe("paused"));
    expect(screen.getByRole("status")).toHaveTextContent(/offline|reconnect/i);
    expect(screen.queryByText(/no vaults available/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
    view.queryClient.clear();
  });

  it("bounds vault renders through pending and rejected reads", async () => {
    const catalogRead = deferred<{ vaults: Vault[] }>();
    const commits = { pending: 0, rejected: 0 };
    let phase: keyof typeof commits = "pending";
    const onRender: ProfilerOnRenderCallback = () => {
      commits[phase] += 1;
      expect(commits[phase]).toBeLessThanOrEqual(12);
    };
    listVaultsMock.mockReturnValue(catalogRead.promise);
    const view = renderVaults(vi.fn().mockResolvedValue(undefined), { profiler: onRender });
    await waitFor(() => expect(view.queryClient.getQueryState(["vaults"])?.status).toBe("pending"));
    phase = "rejected";
    await act(async () => {
      catalogRead.reject(new Error("catalog unavailable"));
      await Promise.allSettled([catalogRead.promise]);
    });
    await waitFor(() => expect(view.queryClient.getQueryState(["vaults"])?.status).toBe("error"));
    expect(screen.getByRole("status")).toHaveTextContent(/unable to load/i);
    expect(commits.pending).toBeGreaterThan(0);
    expect(commits.rejected).toBeGreaterThan(0);
  });

  it.each(["close-button", "escape", "outside"] as const)(
    "blocks and then permits the Radix member %s dismissal path around a deferred save",
    async (path) => {
      const firstSave = deferred<void>();
      const onSave = vi.fn<(userIds: number[]) => Promise<void>>().mockReturnValue(firstSave.promise);
      const onOpenChange = vi.fn<(open: boolean) => void>();
      function Harness() {
        const [open, setOpen] = useState(true);
        const handleOpenChange = (nextOpen: boolean) => {
          onOpenChange(nextOpen);
          setOpen(nextOpen);
        };
        return (
          <>
            <ManageMembersSheet
              group={makeGroup()}
              open={open}
              editorToken={1}
              onOpenChange={handleOpenChange}
              onSave={onSave}
            />
            <output data-testid="members-open">{String(open)}</output>
          </>
        );
      }
      const view = render(
        <QueryClientProvider client={makeQueryClient()}>
          <Harness />
        </QueryClientProvider>,
      );
      const checkbox = await screen.findByLabelText("Select alice");
      fireEvent.click(checkbox);
      const save = screen.getByRole("button", { name: "Save member changes" });
      await waitFor(() => expect(save).not.toBeDisabled());
      fireEvent.click(save);
      await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));

      expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
      await triggerDismissal(path);
      expect(onOpenChange).not.toHaveBeenCalled();
      await waitFor(() => expect(screen.getByTestId("members-open")).toHaveTextContent("true"));
      const memberDialog = screen.getByRole("dialog");
      expect(memberDialog).toBeInTheDocument();
      expect(document.body.contains(memberDialog)).toBe(true);
      expect(screen.getByTestId("members-open")).toHaveTextContent("true");

      await act(async () => {
        firstSave.resolve();
        await firstSave.promise;
      });
      await triggerDismissal(path);
      await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      view.unmount();
      expect(document.body.querySelector('[role="dialog"]')).toBeNull();
    },
  );

  it.each(["close-button", "escape", "outside"] as const)(
    "blocks and then permits the Radix vault %s dismissal path around a deferred save",
    async (path) => {
      const firstSave = deferred<void>();
      const onSave = vi.fn<(access: VaultAccessItem[]) => Promise<void>>().mockReturnValue(firstSave.promise);
      const onOpenChange = vi.fn<(open: boolean) => void>();
      function Harness() {
        const [open, setOpen] = useState(true);
        const handleOpenChange = (nextOpen: boolean) => {
          onOpenChange(nextOpen);
          setOpen(nextOpen);
        };
        return (
          <>
            <ManageVaultsSheet
              group={makeGroup()}
              open={open}
              editorToken={1}
              onOpenChange={handleOpenChange}
              onSave={onSave}
            />
            <output data-testid="vaults-open">{String(open)}</output>
          </>
        );
      }
      const view = render(
        <QueryClientProvider client={makeQueryClient()}>
          <Harness />
        </QueryClientProvider>,
      );
      const checkbox = await screen.findByLabelText("Grant access to Knowledge Vault");
      fireEvent.click(checkbox);
      const save = screen.getByRole("button", { name: "Save vault access changes" });
      await waitFor(() => expect(save).not.toBeDisabled());
      fireEvent.click(save);
      await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));

      expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
      await triggerDismissal(path);
      expect(onOpenChange).not.toHaveBeenCalled();
      await waitFor(() => expect(screen.getByTestId("vaults-open")).toHaveTextContent("true"));
      const vaultDialog = screen.getByRole("dialog");
      expect(vaultDialog).toBeInTheDocument();
      expect(document.body.contains(vaultDialog)).toBe(true);
      expect(screen.getByTestId("vaults-open")).toHaveTextContent("true");

      await act(async () => {
        firstSave.resolve();
        await firstSave.promise;
      });
      await triggerDismissal(path);
      await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      view.unmount();
      expect(document.body.querySelector('[role="dialog"]')).toBeNull();
    },
  );
});
