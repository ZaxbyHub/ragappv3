import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import AdminUsersPage from '@/pages/AdminUsersPage';

// Mock useAuthStore for AdminGuard and user data
vi.mock('@/stores/useAuthStore', () => ({
  useAuthStore: vi.fn((selector) => {
    if (typeof selector === 'function') {
      return selector({
        user: {
          id: 1,
          username: 'admin',
          full_name: 'Admin User',
          role: 'superadmin',
          is_active: true,
        },
        isAuthenticated: true,
        isLoading: false,
      });
    }
    return {
      user: {
        id: 1,
        username: 'admin',
        full_name: 'Admin User',
        role: 'superadmin',
        is_active: true,
      },
      isAuthenticated: true,
      isLoading: false,
    };
  }),
}));

// Mock apiClient
vi.mock('@/lib/api', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: { users: [
      { id: 1, username: 'admin', full_name: 'Admin User', role: 'admin', is_active: true, created_at: '2024-01-01' },
      { id: 2, username: 'john', full_name: 'John Doe', role: 'member', is_active: true, created_at: '2024-01-02' },
      { id: 3, username: 'jane', full_name: 'Jane Smith', role: 'viewer', is_active: false, created_at: '2024-01-03' },
    ], total: 3 } }),
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

// Radix Select cannot be driven in jsdom; render it as a native <select> so
// option labels stay assertable and fireEvent.change works on the role picker.
vi.mock('@/components/ui/select', async () => {
  const React = await import('react');
  const findTriggerProps = (children: React.ReactNode): Record<string, unknown> => {
    let props: Record<string, unknown> = {};
    React.Children.forEach(children, (child: any) => {
      if (!child || typeof child !== 'object') return;
      if (child.type?.__isSelectTrigger) {
        props = child.props ?? {};
      } else if (child.props?.children) {
        const nested = findTriggerProps(child.props.children);
        if (Object.keys(nested).length) props = nested;
      }
    });
    return props;
  };
  const collectItems = (children: React.ReactNode): { value: string; label: React.ReactNode }[] => {
    const items: { value: string; label: React.ReactNode }[] = [];
    React.Children.forEach(children, (child: any) => {
      if (!child || typeof child !== 'object') return;
      if (child.type?.__isSelectItem) {
        items.push({ value: child.props.value, label: child.props.children });
      } else if (child.props?.children) {
        items.push(...collectItems(child.props.children));
      }
    });
    return items;
  };
  const SelectTrigger: any = ({ children }: any) => React.createElement(React.Fragment, null, children);
  SelectTrigger.__isSelectTrigger = true;
  const SelectItem: any = ({ children }: any) => React.createElement(React.Fragment, null, children);
  SelectItem.__isSelectItem = true;
  return {
    Select: ({ value, onValueChange, disabled, children }: any) => {
      const triggerProps = findTriggerProps(children);
      return React.createElement(
        'select',
        {
          value,
          disabled,
          'aria-label': triggerProps['aria-label'],
          id: triggerProps.id,
          onChange: (e: any) => onValueChange?.(e.target.value),
        },
        collectItems(children).map((item) =>
          React.createElement('option', { key: item.value, value: item.value }, item.label)
        )
      );
    },
    SelectTrigger,
    SelectValue: () => null,
    SelectContent: ({ children }: any) => React.createElement(React.Fragment, null, children),
    SelectItem,
  };
});

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
}));

describe('AdminUsersPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the page title', async () => {
    await act(async () => {
      render(<AdminUsersPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('User Management')).toBeInTheDocument();
    });
  });

  it('renders user table with user data', async () => {
    await act(async () => {
      render(<AdminUsersPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
      expect(screen.getByText('john')).toBeInTheDocument();
      expect(screen.getByText('jane')).toBeInTheDocument();
    });
  });

  it('renders user full names', async () => {
    await act(async () => {
      render(<AdminUsersPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Admin User')).toBeInTheDocument();
      expect(screen.getByText('John Doe')).toBeInTheDocument();
      expect(screen.getByText('Jane Smith')).toBeInTheDocument();
    });
  });

  it('displays role select options for each user', async () => {
    await act(async () => {
      render(<AdminUsersPage />);
    });

    await waitFor(() => {
      const roleSelects = document.querySelectorAll('select');
      expect(roleSelects.length).toBeGreaterThan(0);
    });
  });

  it('displays status badges for active/inactive users', async () => {
    await act(async () => {
      render(<AdminUsersPage />);
    });

    await waitFor(() => {
      const activeBadges = screen.getAllByText('Active');
      expect(activeBadges.length).toBeGreaterThan(0);
      expect(screen.getByText('Inactive')).toBeInTheDocument();
    });
  });

  it('renders search input', async () => {
    await act(async () => {
      render(<AdminUsersPage />);
    });

    await waitFor(() => {
      const searchInput = screen.getByPlaceholderText('Search by username or name...');
      expect(searchInput).toBeInTheDocument();
    });
  });

  it('renders delete button for users (superadmin can delete)', async () => {
    await act(async () => {
      render(<AdminUsersPage />);
    });

    await waitFor(() => {
      // Should have delete buttons since logged in user is superadmin
      const deleteButtons = document.querySelectorAll('button[aria-label*="Delete"]');
      expect(deleteButtons.length).toBeGreaterThan(0);
    });
  });

  it('renders table headers', async () => {
    await act(async () => {
      render(<AdminUsersPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Username')).toBeInTheDocument();
      expect(screen.getByText('Full Name')).toBeInTheDocument();
      expect(screen.getByText('Role')).toBeInTheDocument();
      expect(screen.getByText('Status')).toBeInTheDocument();
      expect(screen.getByText('Created')).toBeInTheDocument();
      expect(screen.getByText('Actions')).toBeInTheDocument();
    });
  });

  it('handles empty user list', async () => {
    const api = await import('@/lib/api');
    api.default.get.mockResolvedValueOnce({ data: { users: [], total: 0 } });

    await act(async () => {
      render(<AdminUsersPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('No users found')).toBeInTheDocument();
    });
  });

  it('handles null data without crashing', async () => {
    const api = await import('@/lib/api');
    api.default.get.mockRejectedValueOnce(new Error('Network error'));

    await act(async () => {
      render(<AdminUsersPage />);
    });

    // Should not crash, just show loading state
    expect(document.querySelector('body')).toBeInTheDocument();
  });
});