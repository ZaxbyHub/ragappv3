// REGRESSION TEST: Verify ProfilePage does NOT render vault.is_default badge
// Task 5.5: Removed is_default badge from vault rendering
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import ProfilePage from '@/pages/ProfilePage';

// Use vi.hoisted for mock factory
const { mockChangePassword } = vi.hoisted(() => ({
  mockChangePassword: vi.fn().mockResolvedValue(undefined),
}));

// Default auth store state factory
const defaultAuthState = () => ({
  user: { id: 1, username: 'testuser', full_name: 'Test User', role: 'member' as const },
  isAuthenticated: true,
  isLoading: false,
  updateProfile: vi.fn().mockResolvedValue({}),
});

const { mockUseAuthStore } = vi.hoisted(() => ({
  mockUseAuthStore: vi.fn((selector: any) => {
    const state = defaultAuthState();
    return typeof selector === 'function' ? selector(state) : state;
  }),
}));

vi.mock('@/stores/useAuthStore', () => ({
  useAuthStore: mockUseAuthStore,
}));

// Mock listAccessibleVaults with is_default vaults
// Define inline within the hoisted factory to avoid TDZ
const { mockListAccessibleVaults } = vi.hoisted(() => {
  const mockVaultsWithDefault = [
    { id: 1, name: 'Default Vault', description: 'The default vault', created_at: '2024-01-01', updated_at: '2024-01-01', file_count: 10, memory_count: 5, session_count: 3, org_id: 1, is_default: true },
    { id: 2, name: 'Secondary Vault', description: 'Another vault', created_at: '2024-01-01', updated_at: '2024-01-01', file_count: 5, memory_count: 2, session_count: 1, org_id: 1, is_default: false },
  ];
  return {
    mockListAccessibleVaults: vi.fn().mockResolvedValue({ vaults: mockVaultsWithDefault }),
  };
});

vi.mock('@/lib/api', () => ({
  changePassword: mockChangePassword,
  listOrganizations: vi.fn().mockResolvedValue([]),
  listAccessibleVaults: mockListAccessibleVaults,
  listSessions: vi.fn().mockResolvedValue({ sessions: [] }),
  revokeSession: vi.fn().mockResolvedValue(undefined),
  revokeAllSessions: vi.fn().mockResolvedValue({ access_token: 'token', token_type: 'bearer', expires_in: 900 }),
  setJwtAccessToken: vi.fn(),
}));

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/components/ui/card', () => ({
  Card: ({ children }: any) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: any) => <div data-testid="card-content">{children}</div>,
  CardHeader: ({ children }: any) => <div data-testid="card-header">{children}</div>,
  CardTitle: ({ children }: any) => <h3>{children}</h3>,
  CardDescription: ({ children }: any) => <p>{children}</p>,
}));

vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick, disabled, ...props }: any) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}));

vi.mock('@/components/ui/input', () => ({
  Input: (props: any) => <input {...props} />,
}));

vi.mock('@/components/ui/badge', () => ({
  Badge: ({ children }: any) => <span data-testid="badge">{children}</span>,
}));

vi.mock('@/components/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: any) => <div>{children}</div>,
}));

describe('ProfilePage Vault Access — is_default badge removal (5.5)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuthStore.mockImplementation((selector: any) => {
      const state = defaultAuthState();
      return typeof selector === 'function' ? selector(state) : state;
    });
    mockChangePassword.mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('does NOT render "Default" badge for vault with is_default=true', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    // Wait for the Vault Access section to render
    await waitFor(() => {
      expect(screen.getByText('Vault Access')).toBeInTheDocument();
    });

    // Verify vault names are rendered
    expect(screen.getByText('Default Vault')).toBeInTheDocument();
    expect(screen.getByText('Secondary Vault')).toBeInTheDocument();

    // The critical assertion: "Default" should NOT appear as a badge
    // Since Badge renders as <span data-testid="badge">{children}</span>,
    // we check that no badge contains "Default" text
    const badges = document.querySelectorAll('[data-testid="badge"]');
    const badgeTexts = Array.from(badges).map(badge => badge.textContent);
    expect(badgeTexts).not.toContain('Default');
  });

  it('does NOT render "Default" badge when all vaults have is_default=false', async () => {
    // Override the mock to return vaults without is_default
    mockListAccessibleVaults.mockResolvedValueOnce({
      vaults: [
        { id: 3, name: 'Regular Vault', description: 'Not default', created_at: '2024-01-01', updated_at: '2024-01-01', file_count: 3, memory_count: 1, session_count: 0, org_id: 1, is_default: false },
      ],
    });

    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Vault Access')).toBeInTheDocument();
    });

    expect(screen.getByText('Regular Vault')).toBeInTheDocument();

    // No "Default" badge should exist
    const badges = document.querySelectorAll('[data-testid="badge"]');
    const badgeTexts = Array.from(badges).map(badge => badge.textContent);
    expect(badgeTexts).not.toContain('Default');
  });

  it('renders vault name and file_count correctly without Default badge', async () => {
    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Vault Access')).toBeInTheDocument();
    });

    // Vault name should appear
    expect(screen.getByText('Default Vault')).toBeInTheDocument();

    // File count should appear (file_count > 0 so it renders)
    expect(screen.getByText('10 docs')).toBeInTheDocument();

    // No "Default" badge
    const badges = document.querySelectorAll('[data-testid="badge"]');
    const badgeTexts = Array.from(badges).map(badge => badge.textContent);
    expect(badgeTexts).not.toContain('Default');
  });

  it('renders empty vault list without Default badge', async () => {
    mockListAccessibleVaults.mockResolvedValueOnce({ vaults: [] });

    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Vault Access')).toBeInTheDocument();
    });

    expect(screen.getByText('No vaults accessible.')).toBeInTheDocument();

    // Role "Member" badge still renders (expected), but no "Default" badge for vaults
    const badges = document.querySelectorAll('[data-testid="badge"]');
    const badgeTexts = Array.from(badges).map(badge => badge.textContent);
    // Member badge is expected (role), but no Default badge
    expect(badgeTexts).toContain('Member');
    expect(badgeTexts).not.toContain('Default');
  });

  it('renders vault with is_default but no file_count without Default badge', async () => {
    mockListAccessibleVaults.mockResolvedValueOnce({
      vaults: [
        { id: 4, name: 'Empty Vault', description: 'No files', created_at: '2024-01-01', updated_at: '2024-01-01', file_count: 0, memory_count: 0, session_count: 0, org_id: 1, is_default: true },
      ],
    });

    await act(async () => {
      render(<ProfilePage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Vault Access')).toBeInTheDocument();
    });

    expect(screen.getByText('Empty Vault')).toBeInTheDocument();

    // No "Default" badge even though vault has is_default: true
    const badges = document.querySelectorAll('[data-testid="badge"]');
    const badgeTexts = Array.from(badges).map(badge => badge.textContent);
    expect(badgeTexts).not.toContain('Default');
  });
});
