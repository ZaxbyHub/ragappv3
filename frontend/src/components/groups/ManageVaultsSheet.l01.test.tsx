import type { ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManageVaultsSheet } from "./ManageVaultsSheet";
import type { Group, Vault, VaultAccessItem } from "@/lib/api";

const { listVaultsMock, getGroupVaultsMock } = vi.hoisted(() => ({
  listVaultsMock: vi.fn(),
  getGroupVaultsMock: vi.fn(),
}));

const activeQueryClients: QueryClient[] = [];

function makeQueryClient() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { gcTime: 0, retry: false } },
  });
  activeQueryClients.push(queryClient);
  return queryClient;
}

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
    <input
      type="checkbox"
      {...props}
      onChange={() => onCheckedChange?.()}
    />
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
  }: {
    children: ReactNode;
    disabled?: boolean;
    onValueChange?: (value: string) => void;
  }) => (
    <button
      type="button"
      disabled={disabled}
      aria-label="Permission level for Vault 101"
      onClick={() => onValueChange?.("write")}
    >
      {children}
    </button>
  ),
  SelectContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SelectItem: ({ children }: { children: ReactNode }) => <option>{children}</option>,
  SelectTrigger: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  SelectValue: () => <span />,
}));
vi.mock("lucide-react", () => ({
  FolderOpen: () => <span />,
  Loader2: () => <span />,
  Search: () => <span />,
  Shield: () => <span />,
}));

function makeGroup(id: number, overrides: Partial<Group> = {}): Group {
  return {
    id,
    name: `Group ${id}`,
    description: null,
    created_at: "2024-01-01T00:00:00Z",
    org_id: null,
    organization_name: null,
    ...overrides,
  };
}

function makeVault(id: number): Vault {
  return {
    id,
    name: `Vault ${id}`,
    description: null,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    file_count: 0,
    memory_count: 0,
    session_count: 0,
    org_id: null,
    current_user_permission: "admin",
  };
}

function renderWithClient(
  group: Group,
  onSave: (access: VaultAccessItem[]) => Promise<void>,
  queryClient: QueryClient,
) {
  return render(
    <QueryClientProvider client={queryClient}>
      <ManageVaultsSheet group={group} open onOpenChange={vi.fn()} onSave={onSave} />
    </QueryClientProvider>,
  );
}

describe("ManageVaultsSheet issue #772 acceptance checks", () => {
  beforeEach(() => {
    listVaultsMock.mockReset().mockResolvedValue({ vaults: [makeVault(101)] });
    getGroupVaultsMock.mockReset().mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    activeQueryClients.splice(0).forEach((queryClient) => queryClient.clear());
  });

  it("does not carry group A access into group B save", async () => {
    getGroupVaultsMock.mockImplementation((groupId: number) =>
      Promise.resolve(groupId === 1 ? [{ id: 101, permission: "read" }] : []),
    );
    const onSave = vi.fn<(access: VaultAccessItem[]) => Promise<void>>().mockResolvedValue();
    const queryClient = makeQueryClient();
    const view = renderWithClient(makeGroup(1), onSave, queryClient);

    await waitFor(() => expect(getGroupVaultsMock).toHaveBeenCalledWith(1));
    await waitFor(() => expect(screen.getByLabelText("Grant access to Vault 101")).toBeChecked());

    view.rerender(
      <QueryClientProvider client={queryClient}>
        <ManageVaultsSheet group={makeGroup(2)} open onOpenChange={vi.fn()} onSave={onSave} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(getGroupVaultsMock).toHaveBeenCalledWith(2));
    const save = screen.getByRole("button", { name: "Save vault access changes" });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0]).toHaveLength(0);
  });

  it("disables Save when the group vault-access read fails", async () => {
    getGroupVaultsMock.mockRejectedValue(new Error("vault access unavailable"));
    const queryClient = makeQueryClient();
    renderWithClient(makeGroup(1), vi.fn().mockResolvedValue(undefined), queryClient);

    await waitFor(() => expect(getGroupVaultsMock).toHaveBeenCalledWith(1));
    await waitFor(() => expect(queryClient.getQueryState(["groups", 1, "vaults"])?.status).toBe("error"));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Save vault access changes" })).toBeDisabled();
    });
  });

  it("disables Save while the initial vault-access read has no data", async () => {
    getGroupVaultsMock.mockImplementation(() => new Promise<never>(() => undefined));
    const queryClient = makeQueryClient();
    renderWithClient(makeGroup(1), vi.fn().mockResolvedValue(undefined), queryClient);

    await waitFor(() => expect(getGroupVaultsMock).toHaveBeenCalledWith(1));
    expect(screen.getByRole("button", { name: "Save vault access changes" })).toBeDisabled();
  });

  it("does not discard an unsaved edit when a successful query refetches", async () => {
    getGroupVaultsMock
      .mockResolvedValueOnce([{ id: 101, permission: "read" }])
      .mockResolvedValueOnce([{ id: 101, permission: "write" }]);
    const queryClient = makeQueryClient();
    renderWithClient(makeGroup(1), vi.fn().mockResolvedValue(undefined), queryClient);

    const checkbox = await screen.findByLabelText("Grant access to Vault 101");
    expect(checkbox).toBeChecked();
    fireEvent.click(checkbox);
    expect(checkbox).not.toBeChecked();

    await queryClient.refetchQueries({ queryKey: ["groups", 1, "vaults"] });
    expect(screen.getByLabelText("Grant access to Vault 101")).not.toBeChecked();
  });

  it("keeps the last loaded access and Save enabled when the current refetch fails", async () => {
    getGroupVaultsMock.mockResolvedValueOnce([{ id: 101, permission: "read" }]);
    const queryClient = makeQueryClient();
    const onSave = vi.fn<(access: VaultAccessItem[]) => Promise<void>>().mockResolvedValue(undefined);
    renderWithClient(makeGroup(1), onSave, queryClient);
    const checkbox = await screen.findByLabelText("Grant access to Vault 101");
    fireEvent.click(checkbox);
    expect(checkbox).not.toBeChecked();

    getGroupVaultsMock.mockRejectedValueOnce(new Error("refetch failed"));
    await queryClient.refetchQueries({ queryKey: ["groups", 1, "vaults"] });
    await waitFor(() => expect(queryClient.getQueryState(["groups", 1, "vaults"])?.status).toBe("error"));

    // A failed background refresh preserves the initialized local snapshot;
    // the warning is actionable while Save remains available for that snapshot.
    expect(screen.getByRole("status")).toHaveTextContent(/latest vault-access refresh failed/i);
    expect(screen.getByLabelText("Grant access to Vault 101")).not.toBeChecked();
    const save = screen.getByRole("button", { name: "Save vault access changes" });
    expect(save).not.toBeDisabled();

    fireEvent.click(save);
    await waitFor(() => expect(onSave).toHaveBeenCalledWith([]));
    await waitFor(() => expect(save).not.toBeDisabled());

    getGroupVaultsMock.mockResolvedValueOnce([{ id: 101, permission: "read" }]);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(getGroupVaultsMock).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(screen.queryByText(/latest vault-access refresh failed/i)).not.toBeInTheDocument(),
    );
  });
});
