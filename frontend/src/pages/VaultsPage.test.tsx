import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import * as fs from "fs";
import * as path from "path";

const vaultStoreMock = vi.hoisted(() => ({
  vaults: [
    {
      id: 1,
      name: "Research",
      description: "",
      file_count: 0,
      memory_count: 0,
      session_count: 0,
      current_user_permission: "admin",
    },
    {
      id: 2,
      name: "Admin Vault",
      description: "",
      file_count: 0,
      memory_count: 0,
      session_count: 0,
      current_user_permission: "admin",
    },
    {
      id: 3,
      name: "Write Vault",
      description: "",
      file_count: 0,
      memory_count: 0,
      session_count: 0,
      current_user_permission: "write",
    },
  ],
  loading: false,
  fetchVaults: vi.fn(),
  addVault: vi.fn(),
  editVault: vi.fn(),
  removeVault: vi.fn(),
  activeVaultId: 2,
  setActiveVault: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listOrganizations: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn(() => vaultStoreMock),
}));

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
  Select: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectItem: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectTrigger: ({ children }: { children: React.ReactNode }) => <button>{children}</button>,
  SelectValue: ({ placeholder }: { placeholder?: string }) => <span>{placeholder}</span>,
}));

vi.mock("lucide-react", () => ({
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

describe("VaultsPage permission gating", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("uses each vault permission and default status for edit/delete controls", async () => {
    render(<VaultsPage />);

    expect(await screen.findByText("Admin Vault")).toBeInTheDocument();

    // There are two admin vaults (Research and Admin Vault), so check all Edit/Delete buttons
    const editButtons = screen.getAllByTitle("Edit vault");
    const deleteButtons = screen.getAllByTitle("Delete vault");
    expect(editButtons.length).toBe(2);
    expect(deleteButtons.length).toBe(2);

    for (const button of editButtons) {
      expect(button).not.toBeDisabled();
    }
    for (const button of deleteButtons) {
      expect(button).not.toBeDisabled();
    }

    for (const button of screen.getAllByTitle("Vault admin permission is required")) {
      expect(button).toBeDisabled();
    }
  });
});

describe("VaultsPage isDefaultVault removal (5.4)", () => {
  const vaultsPagePath = path.resolve(__dirname, "VaultsPage.tsx");

  it("does NOT reference isDefaultVault in source code", () => {
    const sourceCode = fs.readFileSync(vaultsPagePath, "utf-8");
    expect(sourceCode).not.toMatch(/isDefaultVault/);
  });

  it("does NOT reference is_default in source code", () => {
    const sourceCode = fs.readFileSync(vaultsPagePath, "utf-8");
    expect(sourceCode).not.toMatch(/is_default/);
  });

  it("does NOT render 'Default' badge for any vault", async () => {
    render(<VaultsPage />);
    await screen.findByText("Admin Vault");

    // Use queryAllByText which returns [] instead of throwing when no elements found
    const badges = screen.queryAllByText("Default");
    expect(badges).toHaveLength(0);
  });

  it("shows 'Vault admin permission is required' title for non-admin vaults", async () => {
    render(<VaultsPage />);
    await screen.findByText("Admin Vault");

    const permissionRequiredButtons = screen.getAllByTitle("Vault admin permission is required");
    expect(permissionRequiredButtons.length).toBeGreaterThan(0);

    for (const button of permissionRequiredButtons) {
      expect(button).toBeDisabled();
    }
  });
});

describe("VaultsPage dialog forms", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("submits the create dialog through its form", async () => {
    render(<VaultsPage />);
    await screen.findByText("Admin Vault");

    fireEvent.click(screen.getByRole("button", { name: /new vault/i }));

    const createNameInput = screen.getByLabelText("Vault Name");
    const form = createNameInput.closest("form");
    expect(form).not.toBeNull();

    fireEvent.change(createNameInput, { target: { value: "New Vault" } });
    fireEvent.submit(form!);

    await waitFor(() => {
      expect(vaultStoreMock.addVault).toHaveBeenCalledWith({
        name: "New Vault",
        description: "",
        org_id: null,
      });
    });
  });

  it("submits the edit dialog through its form", async () => {
    render(<VaultsPage />);
    await screen.findByText("Admin Vault");

    const editButtons = screen.getAllByTitle("Edit vault");
    fireEvent.click(editButtons[0]);

    const editNameInput = screen.getByLabelText("Vault Name");
    const form = editNameInput.closest("form");
    expect(form).not.toBeNull();

    fireEvent.change(editNameInput, { target: { value: "Renamed Vault" } });
    fireEvent.submit(form!);

    await waitFor(() => {
      expect(vaultStoreMock.editVault).toHaveBeenCalledWith(1, {
        name: "Renamed Vault",
        description: "",
      });
    });
  });
});
