/**
 * Tests for ManageVaultsSheet org-scoped vault filtering.
 *
 * The component filters the vault list so that only:
 *   (a) vaults belonging to the same organisation as the group, or
 *   (b) "global" vaults with org_id == null
 * are shown when a group has a non-null org_id.
 *
 * Relates to issue: [Follow-up] PR #39 — Auto-Assign Users: Post-Merge Fixes
 * (org/vault filtering tests).
 */

import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ManageVaultsSheet } from "./ManageVaultsSheet";
import type { Group, Vault, VaultAccessItem } from "@/lib/api";

// ---- helpers ----------------------------------------------------------------

function makeGroup(overrides: Partial<Group> = {}): Group {
  return {
    id: 1,
    name: "Test Group",
    description: null,
    created_at: "2024-01-01T00:00:00Z",
    org_id: 10,
    organization_name: "Acme Corp",
    ...overrides,
  };
}

function makeVault(overrides: Partial<Vault> = {}): Vault {
  return {
    id: 1,
    name: "Vault A",
    description: "desc",
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    file_count: 0,
    memory_count: 0,
    session_count: 0,
    org_id: null,
    current_user_permission: "admin",
    ...overrides,
  };
}

// ---- mocks ------------------------------------------------------------------

const mockListVaults = vi.fn();
const mockGetGroupVaults = vi.fn();

vi.mock("@/lib/api", () => ({
  listVaults: (...args: unknown[]) => mockListVaults(...args),
  getGroupVaults: (...args: unknown[]) => mockGetGroupVaults(...args),
}));

// Minimal UI component stubs so we only test filter logic, not Radix internals.
vi.mock("@/components/ui/sheet", () => ({
  Sheet: ({ children, open }: { children: React.ReactNode; open: boolean }) =>
    open ? <div data-testid="sheet">{children}</div> : null,
  SheetContent: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  SheetDescription: ({ children }: { children: React.ReactNode }) => (
    <p>{children}</p>
  ),
  SheetFooter: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  SheetHeader: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  SheetTitle: ({ children }: { children: React.ReactNode }) => (
    <h2>{children}</h2>
  ),
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({
    children,
    onClick,
    disabled,
  }: {
    children: React.ReactNode;
    onClick?: () => void;
    disabled?: boolean;
  }) => (
    <button onClick={onClick} disabled={disabled}>
      {children}
    </button>
  ),
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => (
    <input {...props} />
  ),
}));

vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({
    id,
    checked,
    onCheckedChange,
    disabled,
  }: {
    id?: string;
    checked?: boolean;
    onCheckedChange?: () => void;
    disabled?: boolean;
  }) => (
    <input
      id={id}
      type="checkbox"
      checked={checked}
      onChange={onCheckedChange}
      disabled={disabled}
    />
  ),
}));

vi.mock("@/components/ui/label", () => ({
  Label: ({
    children,
    htmlFor,
  }: {
    children: React.ReactNode;
    htmlFor?: string;
  }) => <label htmlFor={htmlFor}>{children}</label>,
}));

vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));

vi.mock("@/components/ui/skeleton", () => ({
  Skeleton: () => <div data-testid="skeleton" />,
}));

vi.mock("@/components/ui/select", () => ({
  Select: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  SelectContent: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  SelectItem: ({
    children,
    value,
  }: {
    children: React.ReactNode;
    value: string;
  }) => <option value={value}>{children}</option>,
  SelectTrigger: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  SelectValue: () => <span />,
}));

vi.mock("lucide-react", () => ({
  Loader2: () => <span data-testid="loader" />,
  FolderOpen: () => <span data-testid="folder-icon" />,
  Search: () => <span data-testid="search-icon" />,
  Shield: () => <span data-testid="shield-icon" />,
}));

// ---- test wrapper -----------------------------------------------------------

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

const noop = async (_: VaultAccessItem[]) => {};

// ---- test suite -------------------------------------------------------------

describe("ManageVaultsSheet — org-scoped vault filtering", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetGroupVaults.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows a vault that belongs to the same org as the group", async () => {
    const group = makeGroup({ org_id: 10 });
    const sameOrgVault = makeVault({ id: 1, name: "Same Org Vault", org_id: 10 });
    mockListVaults.mockResolvedValue({ vaults: [sameOrgVault] });

    render(
      <ManageVaultsSheet
        group={group}
        open={true}
        onOpenChange={vi.fn()}
        onSave={noop}
      />,
      { wrapper }
    );

    await waitFor(() => {
      expect(screen.getByText("Same Org Vault")).toBeInTheDocument();
    });
  });

  it("hides a vault that belongs to a different org than the group", async () => {
    const group = makeGroup({ org_id: 10 });
    const otherOrgVault = makeVault({
      id: 2,
      name: "Other Org Vault",
      org_id: 99,
    });
    mockListVaults.mockResolvedValue({ vaults: [otherOrgVault] });

    render(
      <ManageVaultsSheet
        group={group}
        open={true}
        onOpenChange={vi.fn()}
        onSave={noop}
      />,
      { wrapper }
    );

    await waitFor(() => {
      // Vault from a different org must not appear in the list
      expect(screen.queryByText("Other Org Vault")).not.toBeInTheDocument();
    });
  });

  it("always shows global vaults (org_id = null) regardless of group org", async () => {
    const group = makeGroup({ org_id: 10 });
    const globalVault = makeVault({ id: 3, name: "Global Vault", org_id: null });
    mockListVaults.mockResolvedValue({ vaults: [globalVault] });

    render(
      <ManageVaultsSheet
        group={group}
        open={true}
        onOpenChange={vi.fn()}
        onSave={noop}
      />,
      { wrapper }
    );

    await waitFor(() => {
      expect(screen.getByText("Global Vault")).toBeInTheDocument();
    });
  });

  it("shows both global and org-scoped vaults from the group's org together", async () => {
    const group = makeGroup({ org_id: 10 });
    const sameOrgVault = makeVault({ id: 1, name: "Acme Vault", org_id: 10 });
    const globalVault = makeVault({ id: 2, name: "Global Vault", org_id: null });
    const otherOrgVault = makeVault({
      id: 3,
      name: "Rival Vault",
      org_id: 77,
    });
    mockListVaults.mockResolvedValue({
      vaults: [sameOrgVault, globalVault, otherOrgVault],
    });

    render(
      <ManageVaultsSheet
        group={group}
        open={true}
        onOpenChange={vi.fn()}
        onSave={noop}
      />,
      { wrapper }
    );

    await waitFor(() => {
      expect(screen.getByText("Acme Vault")).toBeInTheDocument();
      expect(screen.getByText("Global Vault")).toBeInTheDocument();
      expect(screen.queryByText("Rival Vault")).not.toBeInTheDocument();
    });
  });
});
