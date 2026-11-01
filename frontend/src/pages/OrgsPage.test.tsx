import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act , within } from '@testing-library/react';
import '@testing-library/jest-dom';
import OrgsPage from '@/pages/OrgsPage';
// Issue #771 (UI-ENH-05): the below-sm card list duplicates row
// text next to the desktop table; scope data queries to the table subtree
// (a card-only regression still fails these assertions).
const inTable = () => within(screen.getByRole('table'));


// Mock useAuthStore for AdminGuard and user data
vi.mock('@/stores/useAuthStore', () => ({
  useAuthStore: vi.fn((selector) => {
    if (typeof selector === 'function') {
      return selector({
        user: {
          id: 1,
          username: 'superadmin',
          full_name: 'Super Admin',
          role: 'superadmin',
        },
        isAuthenticated: true,
        isLoading: false,
      });
    }
    return {
      user: {
        id: 1,
        username: 'superadmin',
        full_name: 'Super Admin',
        role: 'superadmin',
      },
      isAuthenticated: true,
      isLoading: false,
    };
  }),
}));

// Mock apiClient
const mockOrgs = [
  {
    id: 1,
    name: 'Acme Corp',
    description: 'Acme organization',
    member_count: 5,
    vault_count: 3,
    created_at: '2024-01-01',
  },
  {
    id: 2,
    name: 'Globex Inc',
    description: null,
    member_count: 10,
    vault_count: 7,
    created_at: '2024-02-15',
  },
];

const mockMembers = [
  { user_id: 1, username: 'alice', full_name: 'Alice Johnson', role: 'admin' as const, joined_at: '2024-01-10' },
  { user_id: 2, username: 'bob', full_name: 'Bob Smith', role: 'member' as const, joined_at: '2024-01-15' },
];

vi.mock('@/lib/api', () => ({
  default: {
    get: vi.fn().mockImplementation((url: string) => {
      if (url.includes('/members')) {
        return Promise.resolve({ data: { members: mockMembers } });
      }
      return Promise.resolve({ data: mockOrgs });
    }),
    post: vi.fn().mockResolvedValue({ data: { id: 3, name: 'New Org', description: null, member_count: 0, vault_count: 0, created_at: '2024-03-01' } }),
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

vi.mock('@/components/ui/badge', () => ({
  Badge: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) => open ? <div data-testid="dialog">{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div data-testid="dialog-content">{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

vi.mock('@/components/auth/RoleGuard', () => ({
  AdminGuard: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  RoleGuard: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

describe('OrgsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the page title', async () => {
    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Organizations')).toBeInTheDocument();
    });
  });

  it('renders the description', async () => {
    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Manage organizations and their members')).toBeInTheDocument();
    });
  });

  it('renders the Create Organization button', async () => {
    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Create Organization')).toBeInTheDocument();
    });
  });

  it('renders org list with org data', async () => {
    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeInTheDocument();
      expect(screen.getByText('Acme organization')).toBeInTheDocument();
      expect(screen.getByText('Globex Inc')).toBeInTheDocument();
    });
  });

  it('renders member and vault count badges', async () => {
    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('5')).toBeInTheDocument();
      expect(screen.getByText('3')).toBeInTheDocument();
      expect(screen.getByText('10')).toBeInTheDocument();
      expect(screen.getByText('7')).toBeInTheDocument();
    });
  });

  it('renders expand/collapse buttons', async () => {
    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      const expandButtons = document.querySelectorAll('button[aria-label="Expand"]');
      expect(expandButtons.length).toBe(2);
    });
  });

  it('renders delete buttons for superadmin', async () => {
    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      const deleteButtons = document.querySelectorAll('button[aria-label*="Delete"]');
      expect(deleteButtons.length).toBe(2);
    });
  });

  it('handles empty org list', async () => {
    const api = await import('@/lib/api');
    api.default.get.mockResolvedValueOnce({ data: [] });

    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      expect(screen.getByText(/no organizations found/i)).toBeInTheDocument();
    });
  });

  it('handles null data without crashing', async () => {
    const api = await import('@/lib/api');
    api.default.get.mockRejectedValueOnce(new Error('Network error'));

    await act(async () => {
      render(<OrgsPage />);
    });

    // Should not crash, just show loading state
    expect(document.querySelector('body')).toBeInTheDocument();
  });

  it('renders role select when org is expanded', async () => {
    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeInTheDocument();
    });

    // Click expand button to expand an org
    const expandButtons = screen.getAllByRole('button', { name: /expand/i });
    await act(async () => {
      expandButtons[0].click();
    });

    // Wait for members to load and display
    await waitFor(() => {
      expect(inTable().getByText('Alice Johnson')).toBeInTheDocument();
    });
  });

  it('renders org members after expanding', async () => {
    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeInTheDocument();
    });

    const expandButtons = screen.getAllByRole('button', { name: /expand/i });
    await act(async () => {
      expandButtons[0].click();
    });

    await waitFor(() => {
      expect(inTable().getByText('Alice Johnson')).toBeInTheDocument();
      expect(inTable().getByText('@alice')).toBeInTheDocument();
      expect(inTable().getByText('Bob Smith')).toBeInTheDocument();
      expect(inTable().getByText('@bob')).toBeInTheDocument();
    });
  });

  it('calls API with correct endpoints on mount', async () => {
    const api = await import('@/lib/api');

    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      expect(api.default.get).toHaveBeenCalledWith('/organizations/');
    });
  });

  it('renders the create dialog form fields', async () => {
    // Open create dialog by clicking the Create Organization button
    await act(async () => {
      render(<OrgsPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Acme Corp')).toBeInTheDocument();
    });

    // The create dialog renders conditionally, we need to open it
    const createButton = screen.getByRole('button', { name: /create organization/i });
    await act(async () => {
      createButton.click();
    });

    // Dialog should be open now
    await waitFor(() => {
      expect(screen.getByTestId('dialog')).toBeInTheDocument();
    });
  });
});