// frontend/src/pages/MemoryPage.m01.test.tsx
// Issue-trace 781-vaultgate-first-run-baseline — acceptance checks C2/C3 (AC1).
//
// The null-vault branch of MemoryPage (MemoryPage.tsx:30-43) tells the user to
// "Choose a vault from the vault selector" but renders NO VaultSelector — the
// selector only mounts inside MemoryPageContent (line ~180), which the guard
// branch never renders. The branch also titles the page "Memories" while the
// populated branch titles it "Memory".
//
// Mirrors the store/UI mocking pattern of MemoryPage.test.tsx (the module-level
// vi.mock set plus the per-test useVaultStore mockImplementation seeding).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import '@testing-library/jest-dom';
import MemoryPage from '@/pages/MemoryPage';

// Hoisted mock functions
const mockFetchVaults = vi.hoisted(() => vi.fn());
const mockSetActiveVault = vi.hoisted(() => vi.fn());
const mockUseMemorySearch = vi.hoisted(() => vi.fn());
const mockUseMemoryCrud = vi.hoisted(() => vi.fn());
const mockUpdateMemory = vi.hoisted(() => vi.fn());
const mockPromoteMemoryToWiki = vi.hoisted(() => vi.fn());
const mockGetMemoryWikiStatus = vi.hoisted(() => vi.fn());

// Mock useVaultStore
vi.mock('@/stores/useVaultStore', () => ({
  useVaultStore: Object.assign(vi.fn((selector) => {
    const state = {
      activeVaultId: null,
      vaults: [],
      loading: false,
      error: null,
      fetchVaults: mockFetchVaults,
      setActiveVault: mockSetActiveVault,
    };
    if (typeof selector === 'function') {
      return selector(state);
    }
    return state;
  }), {
    getState: vi.fn(() => ({
      activeVaultId: null,
      vaults: [],
      fetchVaults: mockFetchVaults,
      setActiveVault: mockSetActiveVault,
    })),
  }),
}));

// Mock useMemorySearch
vi.mock('@/hooks/useMemorySearch', () => ({
  useMemorySearch: mockUseMemorySearch,
}));

// Mock useMemoryCrud
vi.mock('@/hooks/useMemoryCrud', () => ({
  useMemoryCrud: mockUseMemoryCrud,
  getCategoryFromMetadata: vi.fn(() => 'Uncategorized'),
  getTagsFromMetadata: vi.fn(() => []),
  getSourceFromMetadata: vi.fn(() => ''),
  MAX_MEMORY_CONTENT_LENGTH: 10000,
}));

// Mock @/lib/api
vi.mock('@/lib/api', () => ({
  updateMemory: mockUpdateMemory,
  promoteMemoryToWiki: mockPromoteMemoryToWiki,
  getMemoryWikiStatus: mockGetMemoryWikiStatus,
  batchMemoryWikiStatus: vi.fn().mockResolvedValue({}),
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

vi.mock('@/components/ui/skeleton', () => ({
  Skeleton: ({ className }: { className?: string }) => <div data-testid="skeleton" className={className} />,
}));

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ open, children }: { open?: boolean; children: React.ReactNode }) => open ? <div data-testid="dialog">{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div data-testid="dialog-content">{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div data-testid="dialog-header">{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div data-testid="dialog-footer">{children}</div>,
}));

vi.mock('@/components/ui/textarea', () => ({
  Textarea: (props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} />,
}));

vi.mock('@/components/ui/label', () => ({
  Label: ({ children, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) => <label {...props}>{children}</label>,
}));

// AMENDED (CHECK_WRONG, sanctioned pre-implementation, round 2): the original
// mock dropped the `action` prop entirely, but the real EmptyState contract is
// `action?: ReactNode | EmptyStateAction` (EmptyState.tsx isActionObject) — as
// first frozen, no implementation could render a control through the gate's
// empty state. Mirrors the same discrimination as the C6 amendment.
vi.mock('@/components/EmptyState', () => ({
  EmptyState: ({
    title,
    description,
    action,
  }: {
    title: string;
    description?: string;
    action?: { label: string; onClick: () => void } | React.ReactNode;
  }) => {
    const isActionObject = (a: unknown): a is { label: string; onClick: () => void } =>
      typeof a === 'object' && a !== null && 'label' in a && 'onClick' in a;
    return (
      <div data-testid="empty-state" role="status">
        <p data-testid="empty-state-title">{title}</p>
        {description && <p data-testid="empty-state-description">{description}</p>}
        {action &&
          (isActionObject(action) ? (
            <button onClick={action.onClick}>{action.label}</button>
          ) : (
            action
          ))}
      </div>
    );
  },
}));

// The surface under test: on the null-vault branch the page must render a
// vault selector (today it renders none — the acceptance stub makes the gap
// countable). Minimal typed stub, same shape as MemoryPage.test.tsx's mock.
vi.mock('@/components/vault/VaultSelector', () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock('@/components/layout/PageTitleHeader', () => ({
  PageTitleHeader: ({ title }: { title: string }) => <div data-testid="page-title">{title}</div>,
}));

// Lucide icons
vi.mock('lucide-react', () => ({
  Brain: () => <div data-testid="brain-icon">Brain</div>,
  Plus: () => <div data-testid="plus-icon">Plus</div>,
  Search: () => <div data-testid="search-icon">Search</div>,
  Trash2: () => <div data-testid="trash-icon">Trash2</div>,
  Pencil: () => <div data-testid="pencil-icon">Pencil</div>,
  Loader2: () => <div data-testid="loader-icon">Loader2</div>,
  BookOpen: () => <div data-testid="book-icon">BookOpen</div>,
}));

describe('MemoryPage m01 (issue-trace 781-vaultgate-first-run-baseline)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchVaults.mockReset();
    mockFetchVaults.mockResolvedValue(undefined);
    mockSetActiveVault.mockReset();
    mockUpdateMemory.mockReset();
    mockPromoteMemoryToWiki.mockReset();
    mockGetMemoryWikiStatus.mockReset();

    // Default useMemorySearch return value
    mockUseMemorySearch.mockReturnValue({
      memories: [],
      searchQuery: '',
      setSearchQuery: vi.fn(),
      loading: false,
      handleSearch: vi.fn(),
    });

    // Default useMemoryCrud return value
    mockUseMemoryCrud.mockReturnValue({
      isAddDialogOpen: false,
      setIsAddDialogOpen: vi.fn(),
      newMemory: { content: '', category: '', tags: '', source: '' },
      setNewMemory: vi.fn(),
      isSubmitting: false,
      isDeleting: null,
      contentError: '',
      handleContentChange: vi.fn(),
      handleAddMemory: vi.fn(),
      handleKeyDown: vi.fn(),
      handleDeleteMemory: vi.fn(),
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // First-run scenario: the user HAS one accessible vault but has not picked
  // one yet (kv_active_vault_id unset → activeVaultId null).
  const seedNullVaultWithOneAccessible = async () => {
    const { useVaultStore } = await import('@/stores/useVaultStore');
    vi.mocked(useVaultStore).mockImplementation((selector: any) => {
      const state = {
        activeVaultId: null,
        vaults: [{ id: 42, name: 'Accessible Vault' }],
        loading: false,
        error: null,
        fetchVaults: mockFetchVaults,
        setActiveVault: mockSetActiveVault,
      };
      if (typeof selector === 'function') {
        return selector(state);
      }
      return state;
    });
  };

  it('null vault branch renders the vault selector', async () => {
    await seedNullVaultWithOneAccessible();

    await act(async () => {
      render(
        <MemoryRouter>
          <MemoryPage />
        </MemoryRouter>
      );
    });

    // The guard copy tells the user to use "the vault selector", so exactly
    // one selector must be rendered on this branch. At base the count is 0
    // (the selector only mounts inside MemoryPageContent).
    expect(screen.queryAllByTestId('vault-selector').length).toBe(1);
  });

  it('null vault branch uses the Memory page title', async () => {
    await seedNullVaultWithOneAccessible();

    await act(async () => {
      render(
        <MemoryRouter>
          <MemoryPage />
        </MemoryRouter>
      );
    });

    // The populated branch titles the page "Memory"; the singular guard
    // branch must not diverge into "Memories" (at base it does).
    expect(screen.queryAllByText('Memories', { exact: true }).length).toBe(0);
  });
});
