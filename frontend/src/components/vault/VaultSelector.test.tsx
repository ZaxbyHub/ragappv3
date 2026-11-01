import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { VaultSelector } from "@/components/vault/VaultSelector";
import type { Vault } from "@/lib/api";

// Create a mutable mock store state
let mockVaults: Vault[] = [];

// Mock the useVaultStore
vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: Object.assign(vi.fn(() => ({
    vaults: mockVaults,
    activeVaultId: null,
    setActiveVault: vi.fn(),
    fetchVaults: vi.fn(),
    getActiveVault: vi.fn(),
  })),
  {
    getState: vi.fn(() => ({ activeVaultId: null })),
    subscribe: vi.fn(() => () => {}),
  }),
}));

// Mock UI components
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, onClick, "aria-label": ariaLabel, ...props }: { children: React.ReactNode; onClick?: () => void; "aria-label"?: string }) => (
    <button onClick={onClick} aria-label={ariaLabel} data-testid="dropdown-trigger" {...props}>
      {children}
    </button>
  ),
}));

vi.mock("@/components/ui/dropdown-menu", () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div data-testid="dropdown-menu">{children}</div>,
  DropdownMenuTrigger: ({ children, asChild }: { children: React.ReactNode; asChild?: boolean }) => {
    if (asChild && children) {
      return <>{children}</>;
    }
    return <button data-testid="dropdown-trigger">{children}</button>;
  },
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => <div data-testid="dropdown-content">{children}</div>,
  DropdownMenuItem: ({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) => (
    <div role="menuitem" onClick={onClick} data-testid="dropdown-item">{children}</div>
  ),
  DropdownMenuLabel: ({ children }: { children: React.ReactNode }) => <div role="heading">{children}</div>,
  DropdownMenuSeparator: () => <hr data-testid="dropdown-separator" />,
}));

describe("VaultSelector Permission Badge", () => {
  const vaultWithAdmin: Vault = {
    id: 1,
    name: "Vault Alpha",
    description: "First vault",
    created_at: "2024-01-01",
    updated_at: "2024-01-01",
    file_count: 42,
    memory_count: 0,
    session_count: 0,
    org_id: 1,
    current_user_permission: "admin",
  };

  const vaultWithRead: Vault = {
    id: 2,
    name: "Vault Beta",
    description: "Second vault",
    created_at: "2024-01-01",
    updated_at: "2024-01-01",
    file_count: 17,
    memory_count: 0,
    session_count: 0,
    org_id: 1,
    current_user_permission: "read",
  };

  const vaultWithoutPermission: Vault = {
    id: 3,
    name: "Vault Gamma",
    description: "Third vault",
    created_at: "2024-01-01",
    updated_at: "2024-01-01",
    file_count: 8,
    memory_count: 0,
    session_count: 0,
    org_id: 1,
    // current_user_permission is intentionally absent (undefined)
  };

  const vaultWithNullPermission: Vault = {
    id: 4,
    name: "Vault Delta",
    description: "Fourth vault",
    created_at: "2024-01-01",
    updated_at: "2024-01-01",
    file_count: 5,
    memory_count: 0,
    session_count: 0,
    org_id: 1,
    current_user_permission: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockVaults = [];
  });

  describe("permission badge rendering", () => {
    it("renders permission badge when current_user_permission is set to 'admin'", async () => {
      mockVaults = [vaultWithAdmin];

      await act(async () => {
        render(<MemoryRouter><VaultSelector /></MemoryRouter>);
      });

      // The dropdown is closed initially, we need to open it
      const trigger = document.querySelector('[data-testid="dropdown-trigger"]');
      expect(trigger).not.toBeNull();

      // Open the dropdown
      await act(async () => {
        fireEvent.click(trigger!);
      });

      // Check that the dropdown content is rendered
      const dropdownContent = document.querySelector('[data-testid="dropdown-content"]');
      expect(dropdownContent).not.toBeNull();

      // The permission badge text "admin" should be present in the dropdown
      const content = dropdownContent?.textContent || "";
      expect(content).toContain("admin");
      expect(content).toContain("Vault Alpha");
      expect(content).toContain("42");
    });

    it("renders permission badge when current_user_permission is set to 'read'", async () => {
      mockVaults = [vaultWithRead];

      await act(async () => {
        render(<MemoryRouter><VaultSelector /></MemoryRouter>);
      });

      // Open the dropdown
      const trigger = document.querySelector('[data-testid="dropdown-trigger"]');
      await act(async () => {
        fireEvent.click(trigger!);
      });

      // Check that the permission badge text "read" is rendered
      const dropdownContent = document.querySelector('[data-testid="dropdown-content"]');
      const content = dropdownContent?.textContent || "";
      expect(content).toContain("read");
      expect(content).toContain("Vault Beta");
      expect(content).toContain("17");
    });

    it("does not render permission badge when current_user_permission is undefined", async () => {
      mockVaults = [vaultWithoutPermission];

      await act(async () => {
        render(<MemoryRouter><VaultSelector /></MemoryRouter>);
      });

      // Open the dropdown
      const trigger = document.querySelector('[data-testid="dropdown-trigger"]');
      await act(async () => {
        fireEvent.click(trigger!);
      });

      // Check that the dropdown content does NOT contain admin, read, or write
      const dropdownContent = document.querySelector('[data-testid="dropdown-content"]');
      const content = dropdownContent?.textContent || "";

      // The vault name and file count should still be present
      expect(content).toContain("Vault Gamma");
      expect(content).toContain("8");

      // But permission text should not appear (no "admin", "read", "write")
      expect(content).not.toContain("admin");
      expect(content).not.toContain("read");
      expect(content).not.toContain("write");
    });

    it("renders multiple permission badges when multiple vaults have permissions", async () => {
      // Set up store mock with all vaults
      mockVaults = [vaultWithAdmin, vaultWithRead, vaultWithoutPermission];

      await act(async () => {
        render(<MemoryRouter><VaultSelector /></MemoryRouter>);
      });

      // Open the dropdown
      const trigger = document.querySelector('[data-testid="dropdown-trigger"]');
      await act(async () => {
        fireEvent.click(trigger!);
      });

      // Check that both "admin" and "read" permissions appear
      const dropdownContent = document.querySelector('[data-testid="dropdown-content"]');
      const content = dropdownContent?.textContent || "";
      expect(content).toContain("admin");
      expect(content).toContain("read");
      // But vaultWithoutPermission should not have any permission text
      expect(content).not.toContain("Vault Gammaadmin"); // ensure "Gamma" and "admin" are not adjacent
    });

    it("does not render permission badge when current_user_permission is explicitly null", async () => {
      mockVaults = [vaultWithNullPermission];

      await act(async () => {
        render(<MemoryRouter><VaultSelector /></MemoryRouter>);
      });

      // Open the dropdown
      const trigger = document.querySelector('[data-testid="dropdown-trigger"]');
      await act(async () => {
        fireEvent.click(trigger!);
      });

      // Check that the dropdown content does NOT contain permission text
      const dropdownContent = document.querySelector('[data-testid="dropdown-content"]');
      const content = dropdownContent?.textContent || "";

      // The vault name and file count should still be present
      expect(content).toContain("Vault Delta");
      expect(content).toContain("5");

      // But permission text should not appear (null is falsy)
      expect(content).not.toContain("admin");
      expect(content).not.toContain("read");
      expect(content).not.toContain("write");
      expect(content).not.toContain("null");
    });
  });

  describe("permission badge position", () => {
    it("renders permission badge between vault name and file count", async () => {
      mockVaults = [vaultWithAdmin];

      await act(async () => {
        render(<MemoryRouter><VaultSelector /></MemoryRouter>);
      });

      // Open the dropdown
      const trigger = document.querySelector('[data-testid="dropdown-trigger"]');
      await act(async () => {
        fireEvent.click(trigger!);
      });

      // Find ALL dropdown items - first is "All Vaults", second is the actual vault
      const dropdownItems = document.querySelectorAll('[data-testid="dropdown-item"]');
      expect(dropdownItems.length).toBe(2); // "All Vaults" + Vault Alpha

      // Get the text content of the second item (Vault Alpha)
      const vaultAlphaItem = dropdownItems[1];
      const textContent = vaultAlphaItem.textContent || "";

      // Verify the order: name -> permission badge -> file count
      const nameIndex = textContent.indexOf("Vault Alpha");
      const permissionIndex = textContent.indexOf("admin");
      const fileCountIndex = textContent.indexOf("42");

      expect(nameIndex).toBeGreaterThan(-1);
      expect(permissionIndex).toBeGreaterThan(-1);
      expect(fileCountIndex).toBeGreaterThan(-1);
      expect(nameIndex).toBeLessThan(permissionIndex);
      expect(permissionIndex).toBeLessThan(fileCountIndex);
    });
  });
});
