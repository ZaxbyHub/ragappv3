/**
 * Issue #1003 (AC8) — frozen frontend check for vault organization adoption.
 *
 * The Edit Vault dialog offers name and description only at base (the
 * Organization picker exists solely in the create dialog), so an org-less
 * vault can never join an organization from the UI. After the fix, a vault
 * admin opening the Edit dialog for an org-less vault must get an
 * organization assignment control, and submitting it must flow through the
 * update API with the selected `org_id` in the payload.
 *
 * Scope boundary pin: a vault that ALREADY has an organization shows NO
 * organization control in the Edit dialog (org→org reassignment is out of
 * scope for #1003). That boundary is green at base and must stay green.
 *
 * Mock layout mirrors ./VaultsPage.test.tsx (UI primitives stubbed), but the
 * vault store is REAL: @/lib/api is mocked so the store's fetchVaults and
 * editVault run against controlled responses and the update payload can be
 * asserted at the API boundary. The shadcn Select is bridged to a native
 * <select> (role combobox) so the organization control is interactable in
 * jsdom.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import type { Vault, Organization, VaultListResponse } from "@/lib/api";

const ORG_A_ID = 101;
const ORG_B_ID = 102;

const orgA: Organization = {
  id: ORG_A_ID,
  name: "Adopt Me Org",
  description: "",
  slug: "adopt-me",
};

const orgB: Organization = {
  id: ORG_B_ID,
  name: "Other Org",
  description: "",
  slug: "other-org",
};

const orgLessVault: Vault = {
  id: 7,
  name: "Legacy Vault",
  description: "created before organizations existed",
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

const orgOwnedVault: Vault = {
  ...orgLessVault,
  id: 8,
  name: "Org Vault",
  org_id: ORG_B_ID,
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
  }) => <option value={value}>{children}</option>,
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
  Trash2: () => <span>Delete icon</span>,
}));

import VaultsPage from "@/pages/VaultsPage";
import { useVaultStore } from "@/stores/useVaultStore";

describe("VaultsPage Edit dialog organization adoption (#1003)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    useVaultStore.setState({
      vaults: [],
      activeVaultId: null,
      loading: false,
      error: null,
    });
    const vaultList: VaultListResponse = { vaults: [orgLessVault], total: 1 };
    apiMocks.listOrganizations.mockResolvedValue([orgA, orgB]);
    apiMocks.listAccessibleVaults.mockResolvedValue(vaultList);
    apiMocks.listVaults.mockResolvedValue(vaultList);
    apiMocks.updateVault.mockResolvedValue({ ...orgLessVault, org_id: ORG_A_ID });
    apiMocks.createVault.mockResolvedValue(orgLessVault);
    apiMocks.deleteVault.mockResolvedValue(undefined);
  });

  it("offers an organization control for an org-less vault and submits org_id through the update API", async () => {
    render(<VaultsPage />);
    expect(await screen.findByText("Legacy Vault")).toBeInTheDocument();
    // Drain the listOrganizations microtask so the orgs state is committed
    // before the dialog interaction.
    await act(async () => {});

    fireEvent.click(screen.getByTitle("Edit vault"));

    const orgSelect = screen.queryByRole("combobox");
    expect(orgSelect).not.toBeNull();

    fireEvent.change(orgSelect!, { target: { value: String(ORG_A_ID) } });

    const form = screen.getByLabelText("Vault Name").closest("form");
    expect(form).not.toBeNull();
    fireEvent.submit(form!);

    await waitFor(() => {
      expect(apiMocks.updateVault).toHaveBeenCalledWith(
        orgLessVault.id,
        expect.objectContaining({ org_id: ORG_A_ID }),
      );
    });
  });

  it("shows no organization control in the Edit dialog when the vault already has an organization", async () => {
    apiMocks.listAccessibleVaults.mockResolvedValue({
      vaults: [orgOwnedVault],
      total: 1,
    });
    apiMocks.listVaults.mockResolvedValue({ vaults: [orgOwnedVault], total: 1 });

    render(<VaultsPage />);
    expect(await screen.findByText("Org Vault")).toBeInTheDocument();
    await act(async () => {});

    fireEvent.click(screen.getByTitle("Edit vault"));

    expect(screen.queryByRole("combobox")).toBeNull();
  });
});
