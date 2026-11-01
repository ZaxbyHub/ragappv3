/**
 * Issue #1003 — Edit-dialog organization pick must reset per vault.
 *
 * The Edit dialog's organization selection (org-less vaults only, #1003) is
 * component state (`editOrgId`), not per-vault state. `openEditDialog`
 * resets it to null on every open so a selection made for one vault and
 * then CANCELED never leaks into a different vault's edit: without the
 * reset, canceling vault A's dialog after picking org 101 and then
 * submitting a name-only edit of vault B would silently adopt vault B into
 * organization 101.
 *
 * This check exercises the cancel-then-reopen flow in ONE mount: pick an
 * org for org-less vault A, cancel, open org-less vault B, submit a
 * name-only change, and assert the update payload carries NO `org_id` key.
 * It fails if the `setEditOrgId(null)` reset in `openEditDialog` is
 * removed.
 *
 * Mock layout mirrors ./VaultsPage.org-adoption.test.tsx (UI primitives
 * stubbed, real vault store, @/lib/api mocked, shadcn Select bridged to a
 * native <select>).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import type { Vault, Organization, VaultListResponse } from "@/lib/api";

const ORG_A_ID = 101;

const orgA: Organization = {
  id: ORG_A_ID,
  name: "Picked Then Canceled Org",
  description: "",
  slug: "picked-then-canceled",
};

const vaultA: Vault = {
  id: 7,
  name: "First Org-less Vault",
  description: "dialog opened, org picked, then canceled",
  created_at: "2023-09-01T08:00:00Z",
  updated_at: "2024-05-01T12:00:00Z",
  file_count: 3,
  memory_count: 1,
  session_count: 2,
  org_id: null,
  current_user_permission: "admin",
  effective_enrichment_enabled: false,
  effective_multimodal_enabled: false,
};

const vaultB: Vault = {
  ...vaultA,
  id: 8,
  name: "Second Org-less Vault",
  description: "name-only edit submitted after the cancel",
};

const { apiMocks } = vi.hoisted(() => ({
  apiMocks: {
    listOrganizations: vi.fn(),
    listAccessibleVaults: vi.fn(),
    listVaults: vi.fn(),
    updateVault: vi.fn(),
    createVault: vi.fn(),
    deleteVault: vi.fn(),
  },
}));

vi.mock("@/lib/api", () => apiMocks);

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock("@/components/ui/card", () => ({
  Card: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  CardContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  CardDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  CardHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({
    children,
    disabled,
    title,
    onClick,
    type,
  }: {
    children: React.ReactNode;
    disabled?: boolean;
    title?: string;
    onClick?: () => void;
    type?: "button" | "submit" | "reset";
  }) => (
    <button disabled={disabled} title={title} onClick={onClick} type={type}>
      {children}
    </button>
  ),
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock("@/components/ui/label", () => ({
  Label: ({ children, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) => (
    <label {...props}>{children}</label>
  ),
}));

vi.mock("@/components/ui/badge", () => ({
  Badge: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock("@/components/ui/skeleton", () => ({
  Skeleton: () => <div />,
}));

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) =>
    open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h3>{children}</h3>,
}));

vi.mock("@/components/ui/select", () => ({
  // Bridge the shadcn/Radix Select to a native <select> (role "combobox")
  // so jsdom can read and drive the organization control.
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value?: string | null;
    onValueChange?: (value: string) => void;
    children?: React.ReactNode;
  }) => (
    <select
      aria-label="Organization"
      value={value ?? ""}
      onChange={(e) => onValueChange?.(e.target.value)}
    >
      {children}
    </select>
  ),
  SelectContent: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
  SelectItem: ({
    value,
    children,
  }: {
    value: string;
    children?: React.ReactNode;
  }) => (
    <option value={value}>{children}</option>
  ),
  SelectTrigger: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
  SelectValue: () => null,
}));

vi.mock("lucide-react", () => ({
  AlertCircle: () => <span />,
  Brain: () => <span />,
  Database: () => <span />,
  FileText: () => <span />,
  Loader2: () => <span />,
  MessageSquare: () => <span />,
  Pencil: () => <span>Edit icon</span>,
  Plus: () => <span />,
  Trash2: () => <span />,
}));

import VaultsPage from "@/pages/VaultsPage";
import { useVaultStore } from "@/stores/useVaultStore";

describe("VaultsPage Edit dialog organization pick resets per vault (#1003)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    useVaultStore.setState({
      vaults: [],
      activeVaultId: null,
      loading: false,
      error: null,
    });
    const vaultList: VaultListResponse = { vaults: [vaultA, vaultB], total: 2 };
    apiMocks.listOrganizations.mockResolvedValue([orgA]);
    apiMocks.listAccessibleVaults.mockResolvedValue(vaultList);
    apiMocks.listVaults.mockResolvedValue(vaultList);
    apiMocks.updateVault.mockResolvedValue(vaultB);
    apiMocks.createVault.mockResolvedValue(vaultA);
    apiMocks.deleteVault.mockResolvedValue(undefined);
  });

  it("does not leak a canceled organization pick into another vault's name-only edit", async () => {
    render(<VaultsPage />);
    expect(await screen.findByText("Second Org-less Vault")).toBeInTheDocument();
    // Drain the listOrganizations microtask so the orgs state is committed
    // before the dialog interactions.
    await act(async () => {});

    // Vault A: open Edit, pick org 101, then CANCEL — the pick must not
    // survive the cancel into any later dialog.
    const editButtons = screen.getAllByTitle("Edit vault");
    expect(editButtons.length).toBe(2);
    fireEvent.click(editButtons[0]);

    const orgSelect = screen.getByRole("combobox");
    fireEvent.change(orgSelect, { target: { value: String(ORG_A_ID) } });
    fireEvent.click(screen.getByText("Cancel"));

    // Vault B (same mount): open Edit and submit a name-only change.
    fireEvent.click(screen.getAllByTitle("Edit vault")[1]);

    const form = screen.getByLabelText("Vault Name").closest("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form!);

    await waitFor(() => {
      expect(apiMocks.updateVault).toHaveBeenCalledTimes(1);
    });
    const [calledVaultId, payload] = apiMocks.updateVault.mock.calls[0];
    expect(calledVaultId).toBe(vaultB.id);
    expect(payload).not.toHaveProperty("org_id");
    expect(payload).toEqual({
      name: vaultB.name,
      description: vaultB.description,
    });
  });
});
