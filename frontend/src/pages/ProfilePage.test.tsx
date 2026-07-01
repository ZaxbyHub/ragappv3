import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ProfilePage from '@/pages/ProfilePage';

const {
  mockListOrganizations,
  mockListAccessibleVaults,
  mockListSessions,
  mockRevokeSession,
  mockRevokeAllSessions,
  mockSetJwtAccessToken,
} = vi.hoisted(() => ({
  mockListOrganizations: vi.fn(),
  mockListAccessibleVaults: vi.fn(),
  mockListSessions: vi.fn(),
  mockRevokeSession: vi.fn(),
  mockRevokeAllSessions: vi.fn(),
  mockSetJwtAccessToken: vi.fn(),
}));

// Mock useAuthStore
vi.mock('@/stores/useAuthStore', () => ({
  useAuthStore: Object.assign(vi.fn((selector) => {
    const mockState = {
      user: {
        id: 1,
        username: 'testuser',
        full_name: 'Test User',
        role: 'member',
      },
      isAuthenticated: true,
      isLoading: false,
      updateProfile: vi.fn().mockResolvedValue({}),
    };
    if (typeof selector === 'function') {
      return selector(mockState);
    }
    return mockState;
  }), { setState: vi.fn() }),
}));

vi.mock('@/lib/api', () => ({
  changePassword: vi.fn().mockResolvedValue(undefined),
  listOrganizations: mockListOrganizations,
  listAccessibleVaults: mockListAccessibleVaults,
  listSessions: mockListSessions,
  revokeSession: mockRevokeSession,
  revokeAllSessions: mockRevokeAllSessions,
  setJwtAccessToken: mockSetJwtAccessToken,
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

vi.mock('@/components/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

describe('ProfilePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListOrganizations.mockResolvedValue([]);
    mockListAccessibleVaults.mockResolvedValue({ vaults: [] });
    mockListSessions.mockResolvedValue({
      sessions: [
        {
          id: '1',
          user_id: 1,
          user_agent: 'Current Browser',
          ip_address: '127.0.0.1',
          created_at: '2026-06-27T12:00:00Z',
          expires_at: '2026-07-27T12:00:00Z',
          is_current: true,
        },
        {
          id: '2',
          user_id: 1,
          user_agent: 'Old Browser',
          ip_address: '192.0.2.10',
          created_at: '2026-06-20T12:00:00Z',
          expires_at: '2026-07-20T12:00:00Z',
          is_current: false,
        },
      ],
    });
    mockRevokeSession.mockResolvedValue(undefined);
    mockRevokeAllSessions.mockResolvedValue({
      access_token: 'rotated-token',
      token_type: 'bearer',
      expires_in: 900,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the page title', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    expect(screen.getByText('Profile')).toBeInTheDocument();
  });

  it('renders the page description', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    expect(screen.getByText('Manage your account settings')).toBeInTheDocument();
  });

  it('renders Profile Information section', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    expect(screen.getByText('Profile Information')).toBeInTheDocument();
    expect(screen.getByText('Update your personal information')).toBeInTheDocument();
  });

  it('renders Change Password section', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    expect(screen.getByRole('heading', { name: /change password/i })).toBeInTheDocument();
    expect(screen.getByText('Update your account password')).toBeInTheDocument();
  });

  it('renders active sessions and protects the current session from direct revoke', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => expect(screen.getByText('Active Sessions')).toBeInTheDocument());
    expect(screen.getByText('Current Browser')).toBeInTheDocument();
    expect(screen.getByText('Old Browser')).toBeInTheDocument();
    expect(screen.getByText('Current')).toBeInTheDocument();

    const revokeButtons = screen.getAllByRole('button', { name: /revoke/i });
    expect(revokeButtons[0]).toBeDisabled();
    expect(revokeButtons[1]).not.toBeDisabled();
  });

  it('revokes another session and refreshes the session list', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => expect(screen.getByText('Old Browser')).toBeInTheDocument());
    const revokeButtons = screen.getAllByRole('button', { name: /revoke/i });

    await act(async () => {
      fireEvent.click(revokeButtons[1]);
    });

    expect(mockRevokeSession).toHaveBeenCalledWith(2);
    expect(mockListSessions).toHaveBeenCalledTimes(2);
  });

  it('renders username field (disabled)', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    const usernameInput = screen.getByLabelText('Username');
    expect(usernameInput).toBeInTheDocument();
    expect(usernameInput).toHaveValue('testuser');
    expect(usernameInput).toBeDisabled();
  });

  it('renders full name field', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    const fullNameInput = screen.getByLabelText('Full name');
    expect(fullNameInput).toBeInTheDocument();
    expect(fullNameInput).toHaveValue('Test User');
  });

  it('renders role badge', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    expect(screen.getByText('Member')).toBeInTheDocument();
  });

  it('renders Save Changes button', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    expect(screen.getByText('Save Changes')).toBeInTheDocument();
  });

  it('renders password form fields', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    expect(screen.getByLabelText('Current password')).toBeInTheDocument();
    expect(screen.getByLabelText('New password')).toBeInTheDocument();
    expect(screen.getByLabelText('Confirm new password')).toBeInTheDocument();
  });

  it('renders Change Password button', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    const buttons = screen.getAllByText('Change Password');
    expect(buttons.length).toBeGreaterThan(0);
  });

  it('password fields are password type', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    expect(screen.getByLabelText('Current password')).toHaveAttribute('type', 'password');
    expect(screen.getByLabelText('New password')).toHaveAttribute('type', 'password');
    expect(screen.getByLabelText('Confirm new password')).toHaveAttribute('type', 'password');
  });

  it('Save Changes button is disabled when name is unchanged', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    const saveButton = screen.getByRole('button', { name: /save changes/i });
    expect(saveButton).toBeDisabled();
  });

  it('Change Password button is disabled when fields are empty', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    const changePasswordButton = document.querySelector('button[type="submit"]');
    // There are two submit buttons, the password one should be in the second form
    // Check that Change Password button is disabled
    const buttons = screen.getAllByRole('button');
    const passwordButton = buttons.find(b => b.textContent?.includes('Change Password'));
    expect(passwordButton).toBeDisabled();
  });

  it('handles user null state', async () => {
    const { useAuthStore } = await import('@/stores/useAuthStore');
    vi.mocked(useAuthStore).mockImplementation((selector: any) => {
      const state = {
        user: null,
        isAuthenticated: false,
        isLoading: false,
        updateProfile: vi.fn(),
      };
      if (typeof selector === 'function') {
        return selector(state);
      }
      return state;
    });

    await act(async () => {
      render(<ProfilePage />);
    });

    // Should render a loading spinner when user is null
    expect(document.querySelector('[role="status"]')).toBeInTheDocument();
  });

  it('does not crash with null full_name', async () => {
    const { useAuthStore } = await import('@/stores/useAuthStore');
    vi.mocked(useAuthStore).mockImplementation((selector: any) => {
      const state = {
        user: {
          id: 1,
          username: 'testuser',
          full_name: null,
          role: 'member',
        },
        isAuthenticated: true,
        isLoading: false,
        updateProfile: vi.fn(),
      };
      if (typeof selector === 'function') {
        return selector(state);
      }
      return state;
    });

    await act(async () => {
      render(<ProfilePage />);
    });

    expect(screen.getByText('Profile')).toBeInTheDocument();
  });

  it('renders protected route wrapper', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    // ProfilePage is wrapped in ProtectedRoute
    // The mock renders children, so the content should be visible
    expect(screen.getByText('Profile')).toBeInTheDocument();
  });

  it('has correct save button disabled when name is empty', async () => {
    const { useAuthStore } = await import('@/stores/useAuthStore');
    vi.mocked(useAuthStore).mockImplementation((selector: any) => {
      const state = {
        user: {
          id: 1,
          username: 'testuser',
          full_name: '',
          role: 'member',
        },
        isAuthenticated: true,
        isLoading: false,
        updateProfile: vi.fn(),
      };
      if (typeof selector === 'function') {
        return selector(state);
      }
      return state;
    });

    await act(async () => {
      render(<ProfilePage />);
    });

    const saveButton = screen.getByRole('button', { name: /save changes/i });
    expect(saveButton).toBeDisabled();
  });

  it('renders empty vault list gracefully when listAccessibleVaults rejects', async () => {
    mockListAccessibleVaults.mockRejectedValue(new Error('Network error'));

    await act(async () => {
      render(<ProfilePage />);
    });

    // Wait for loading to finish (Promise.allSettled resolves)
    await waitFor(() => {
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
    });

    // Page renders without crashing
    expect(screen.getByText('Profile')).toBeInTheDocument();

    // Vault section renders empty state due to graceful degradation
    expect(screen.getByText('Vault Access')).toBeInTheDocument();
    expect(screen.getByText('No vaults accessible.')).toBeInTheDocument();
  });
});
