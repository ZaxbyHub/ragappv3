/**
 * Issue #774 L03 red checkpoint — VaultsPage's mount effect swallows the
 * listOrganizations failure entirely (`.catch(() => {})`): no toast, no
 * state, nothing. A failed orgs load must surface somewhere.
 *
 * Mirrors the mock layout of ./VaultsPage.test.tsx (vault store mock with a
 * successful vault list; @/lib/api mocked so listOrganizations can reject).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import "@testing-library/jest-dom";

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
  ],
  loading: false,
  error: null as string | null,
  fetchVaults: vi.fn().mockResolvedValue(undefined),
  addVault: vi.fn(),
  editVault: vi.fn(),
  removeVault: vi.fn(),
  activeVaultId: 1,
  setActiveVault: vi.fn(),
}));

const { mockListOrganizations } = vi.hoisted(() => ({
  mockListOrganizations: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listOrganizations: mockListOrganizations,
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
  AlertCircle: () => <span />,
  Brain: () => <span />,
  Database: () => <span />,
  FileText: () => <span />,
  Loader2: () => <span />,
  MessageSquare: () => <span />,
  Pencil: () => <span />,
  Plus: () => <span />,
  Trash2: () => <span />,
}));

import VaultsPage from "@/pages/VaultsPage";
import { toast } from "sonner";

describe("issue 774 VaultsPage organizations load failure", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListOrganizations.mockRejectedValue(new Error("organizations endpoint down"));
  });

  it("surfaces an organizations load failure", async () => {
    render(<VaultsPage />);

    // Vault list resolves successfully (mirrors the success path).
    expect(await screen.findByText("Research")).toBeInTheDocument();

    // Let the listOrganizations rejection settle through the effect.
    await act(async () => {
      await Promise.resolve();
    });

    const toastErrorCalls = vi.mocked(toast.error).mock.calls.length;
    const alerts = screen.queryAllByRole("alert").length;
    expect(toastErrorCalls + alerts).toBeGreaterThan(0);
  });
});
