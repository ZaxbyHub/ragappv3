import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import ProfilePage from '@/pages/ProfilePage';

const {
  mockListOrganizations,
  mockListAccessibleVaults,
  mockListVaults,
  mockListSessions,
  mockRevokeSession,
  mockRevokeAllSessions,
  mockSetJwtAccessToken,
} = vi.hoisted(() => ({
  mockListOrganizations: vi.fn(),
  mockListAccessibleVaults: vi.fn(),
  mockListVaults: vi.fn(),
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
  listVaults: mockListVaults,
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

// Mock TestModeContext to ensure non-test-mode path runs
vi.mock('@/fixtures/TestModeContext', () => ({
  useTestMode: vi.fn(() => false),
  TestModeProvider: ({ children }: { children: React.ReactNode }) => children,
}));

describe('ProfilePage listAccessibleVaults', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListOrganizations.mockResolvedValue([]);
    mockListAccessibleVaults.mockResolvedValue({ vaults: [] });
    mockListSessions.mockResolvedValue({ sessions: [] });
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

  // Task 2.1: Verify ProfilePage calls listAccessibleVaults on mount
  it('calls listAccessibleVaults on mount', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(mockListAccessibleVaults).toHaveBeenCalledTimes(1);
    });
  });

  it('calls listAccessibleVaults (NOT listVaults) on mount', async () => {
    // ProfilePage should call listAccessibleVaults, not the deprecated listVaults
    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(mockListAccessibleVaults).toHaveBeenCalledTimes(1);
      expect(mockListVaults).not.toHaveBeenCalled();
    });
  });

  // Task 2.1: Verify vaults section renders correctly for all role types
  it('renders vault access section with vaults returned by listAccessibleVaults', async () => {
    const mockVaults = [
      {
        id: 1,
        name: 'Engineering Docs',
        description: 'Technical specs',
        created_at: '2024-01-01T00:00:00Z',
        updated_at: '2024-06-01T00:00:00Z',
        file_count: 42,
        memory_count: 10,
        session_count: 5,
        org_id: 1,
        effective_enrichment_enabled: false,
      },
      {
        id: 2,
        name: 'Legal Policies',
        description: 'Compliance docs',
        created_at: '2024-02-01T00:00:00Z',
        updated_at: '2024-06-15T00:00:00Z',
        file_count: 15,
        memory_count: 3,
        session_count: 2,
        org_id: 1,
        effective_enrichment_enabled: false,
      },
    ];

    mockListAccessibleVaults.mockResolvedValue({ vaults: mockVaults });

    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Engineering Docs')).toBeInTheDocument();
      expect(screen.getByText('42 docs')).toBeInTheDocument();
      expect(screen.getByText('Legal Policies')).toBeInTheDocument();
      expect(screen.getByText('15 docs')).toBeInTheDocument();
    });
  });

  it('renders Vault Access card with correct title and description', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Vault Access')).toBeInTheDocument();
      expect(screen.getByText('Knowledge vaults you can access')).toBeInTheDocument();
    });
  });

  it('renders "No vaults accessible" when listAccessibleVaults returns empty vaults', async () => {
    mockListAccessibleVaults.mockResolvedValue({ vaults: [] });

    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(screen.getByText('No vaults accessible.')).toBeInTheDocument();
    });
  });

  it('renders vault names with correct formatting', async () => {
    const mockVaults = [
      {
        id: 1,
        name: 'Alpha Vault',
        description: 'Alpha desc',
        created_at: '2024-01-01T00:00:00Z',
        updated_at: '2024-06-01T00:00:00Z',
        file_count: 5,
        memory_count: 0,
        session_count: 0,
        org_id: 1,
        effective_enrichment_enabled: false,
      },
    ];

    mockListAccessibleVaults.mockResolvedValue({ vaults: mockVaults });

    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      // Vault name should be rendered as a <span> inside the list item
      expect(screen.getByText('Alpha Vault')).toBeInTheDocument();
      // file_count=5 means "5 docs" should appear
      expect(screen.getByText('5 docs')).toBeInTheDocument();
    });
  });

  it('handles vaults with zero file_count gracefully', async () => {
    const mockVaults = [
      {
        id: 1,
        name: 'Empty Vault',
        description: 'No files yet',
        created_at: '2024-01-01T00:00:00Z',
        updated_at: '2024-06-01T00:00:00Z',
        file_count: 0,
        memory_count: 0,
        session_count: 0,
        org_id: 1,
        effective_enrichment_enabled: false,
      },
    ];

    mockListAccessibleVaults.mockResolvedValue({ vaults: mockVaults });

    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Empty Vault')).toBeInTheDocument();
    });

    // "0 docs" should NOT appear — the condition is vault.file_count > 0
    expect(screen.queryByText('0 docs')).not.toBeInTheDocument();
  });

  it('sets vaults state from listAccessibleVaults response', async () => {
    const mockVaults = [
      {
        id: 99,
        name: 'Special Vault',
        description: 'Special desc',
        created_at: '2024-01-01T00:00:00Z',
        updated_at: '2024-06-01T00:00:00Z',
        file_count: 7,
        memory_count: 2,
        session_count: 1,
        org_id: 2,
        effective_enrichment_enabled: false,
      },
    ];

    mockListAccessibleVaults.mockResolvedValue({ vaults: mockVaults });

    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(mockListAccessibleVaults).toHaveBeenCalledWith();
      expect(screen.getByText('Special Vault')).toBeInTheDocument();
      expect(screen.getByText('7 docs')).toBeInTheDocument();
    });
  });
});
