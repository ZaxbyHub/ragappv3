import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { VaultMembersPanel } from '@/components/VaultMembersPanel';

// Mock apiClient
vi.mock('@/lib/api', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: { members: [
      { user_id: 1, username: 'alice', full_name: 'Alice Johnson', permission: 'admin', granted_at: '2024-01-01' },
      { user_id: 2, username: 'bob', full_name: 'Bob Smith', permission: 'read', granted_at: '2024-01-02' },
    ], total: 2 } }),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

// Mock sonner toast
vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

// Mock UI components
vi.mock('@/components/ui/card', () => ({
  Card: ({ children }: { children: React.ReactNode }) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: { children: React.ReactNode }) => <div data-testid="card-content">{children}</div>,
  CardHeader: ({ children }: { children: React.ReactNode }) => <div data-testid="card-header">{children}</div>,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h3>{children}</h3>,
  CardDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
}));

vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick, disabled, ...props }: { children: React.ReactNode; onClick?: () => void; disabled?: boolean }) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}));

vi.mock('@/components/ui/input', () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) => open ? <div data-testid="dialog">{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div data-testid="dialog-content">{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

describe('VaultMembersPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the panel title', async () => {
    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /vault members/i })).toBeInTheDocument();
    });
  });

  it('renders the description', async () => {
    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    await waitFor(() => {
      expect(screen.getByText('Manage who has access to this vault')).toBeInTheDocument();
    });
  });

  it('renders the add member form', async () => {
    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    await waitFor(() => {
      const userIdInput = screen.getByPlaceholderText('Enter user ID...');
      expect(userIdInput).toBeInTheDocument();
    });
  });

  it('renders permission select dropdown', async () => {
    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    await waitFor(() => {
      const permSelects = document.querySelectorAll('select');
      expect(permSelects.length).toBeGreaterThan(0);
    });
  });

  it('renders Add button', async () => {
    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /add/i })).toBeInTheDocument();
    });
  });

  it('renders member list table', async () => {
    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    await waitFor(() => {
      const tableHeaders = screen.getAllByRole('columnheader');
      expect(tableHeaders.length).toBe(4);
    });
  });

  it('renders member data', async () => {
    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    await waitFor(() => {
      expect(screen.getByText('Alice Johnson')).toBeInTheDocument();
      expect(screen.getByText('@alice')).toBeInTheDocument();
      expect(screen.getByText('Bob Smith')).toBeInTheDocument();
      expect(screen.getByText('@bob')).toBeInTheDocument();
    });
  });

  it('renders permission labels', async () => {
    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    await waitFor(() => {
      // "Read" appears in the dropdown options AND the form permission dropdown
      const readOptions = screen.getAllByText('Read');
      expect(readOptions.length).toBeGreaterThan(0);
      // "Admin" appears in the permission selects
      const adminOptions = screen.getAllByText('Admin');
      expect(adminOptions.length).toBeGreaterThan(0);
    });
  });

  it('renders remove buttons for members', async () => {
    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    await waitFor(() => {
      const removeButtons = document.querySelectorAll('button[aria-label*="Remove"]');
      expect(removeButtons.length).toBe(2);
    });
  });

  it('handles empty members list', async () => {
    const { default: apiClient } = await import('@/lib/api');
    vi.mocked(apiClient.get).mockResolvedValueOnce({ data: { members: [], total: 0 } });

    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    await waitFor(() => {
      expect(screen.getByText(/no members yet/i)).toBeInTheDocument();
    });
  });

  it('handles null data without crashing', async () => {
    const { default: apiClient } = await import('@/lib/api');
    vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('Network error'));

    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    // Should not crash, should still render the panel
    expect(document.querySelector('[data-testid="card"]')).toBeInTheDocument();
  });

  it('uses the correct vaultId in API calls', async () => {
    const { default: apiClient } = await import('@/lib/api');

    await act(async () => {
      render(<VaultMembersPanel vaultId={42} />);
    });

    await waitFor(() => {
      expect(apiClient.get).toHaveBeenCalledWith('/vaults/42/members');
    });
  });
});