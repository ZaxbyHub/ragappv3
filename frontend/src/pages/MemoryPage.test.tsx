import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
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

vi.mock('@/components/EmptyState', () => ({
  EmptyState: ({ title, description }: { title: string; description?: string }) => (
    <div data-testid="empty-state" role="status">
      <p data-testid="empty-state-title">{title}</p>
      {description && <p data-testid="empty-state-description">{description}</p>}
    </div>
  ),
}));

vi.mock('@/components/vault/VaultSelector', () => ({
  VaultSelector: () => <div data-testid="vault-selector">VaultSelector</div>,
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

describe('MemoryPage', () => {
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

  // Task 2.3: Verify EmptyState is rendered and NO API calls when activeVaultId is null
  describe('when activeVaultId is null', () => {
    it('renders EmptyState with correct title', async () => {
      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
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
      });

      await act(async () => {
        render(<MemoryPage />);
      });

      await waitFor(() => {
        expect(screen.getByTestId('empty-state-title')).toBeInTheDocument();
        expect(screen.getByTestId('empty-state-title')).toHaveTextContent('Select a vault');
      });
    });

    it('renders EmptyState with correct description', async () => {
      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
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
      });

      await act(async () => {
        render(<MemoryPage />);
      });

      await waitFor(() => {
        expect(screen.getByTestId('empty-state-description')).toHaveTextContent(
          'Choose a vault from the vault selector to view its memories.'
        );
      });
    });

    it('does NOT render MemoryPageContent when activeVaultId is null', async () => {
      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
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
      });

      await act(async () => {
        render(<MemoryPage />);
      });

      // VaultSelector and "Add Memory" button are part of MemoryPageContent, not the guard
      expect(screen.queryByTestId('vault-selector')).not.toBeInTheDocument();
      expect(screen.queryByText('Add Memory')).not.toBeInTheDocument();
    });

    it('does NOT call useMemorySearch when activeVaultId is null', async () => {
      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
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
      });

      mockUseMemorySearch.mockClear();

      await act(async () => {
        render(<MemoryPage />);
      });

      // useMemorySearch should never be called because MemoryPageContent is not rendered
      expect(mockUseMemorySearch).not.toHaveBeenCalled();
    });

    it('does NOT call useMemoryCrud when activeVaultId is null', async () => {
      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
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
      });

      mockUseMemoryCrud.mockClear();

      await act(async () => {
        render(<MemoryPage />);
      });

      // useMemoryCrud should never be called because MemoryPageContent is not rendered
      expect(mockUseMemoryCrud).not.toHaveBeenCalled();
    });
  });

  // Task 2.3: Verify MemoryPageContent renders normally when a vault IS selected
  describe('when a vault is selected (activeVaultId is not null)', () => {
    it('renders MemoryPageContent with vault selector and add button', async () => {
      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
        const state = {
          activeVaultId: 42,
          vaults: [{ id: 42, name: 'Test Vault' }],
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

      await act(async () => {
        render(<MemoryPage />);
      });

      await waitFor(() => {
        expect(screen.getByTestId('vault-selector')).toBeInTheDocument();
        expect(screen.getByText('Add Memory')).toBeInTheDocument();
      });
    });

    it('renders page title "Memory"', async () => {
      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
        const state = {
          activeVaultId: 1,
          vaults: [{ id: 1, name: 'My Vault' }],
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

      await act(async () => {
        render(<MemoryPage />);
      });

      await waitFor(() => {
        expect(screen.getByTestId('page-title')).toHaveTextContent('Memory');
      });
    });

    it('renders EmptyState for no memories when memory list is empty', async () => {
      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
        const state = {
          activeVaultId: 1,
          vaults: [{ id: 1, name: 'My Vault' }],
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

      mockUseMemorySearch.mockReturnValue({
        memories: [],
        searchQuery: '',
        setSearchQuery: vi.fn(),
        loading: false,
        handleSearch: vi.fn(),
      });

      await act(async () => {
        render(<MemoryPage />);
      });

      await waitFor(() => {
        expect(screen.getByText('No memories yet')).toBeInTheDocument();
      });
    });

    it('does NOT render the guard EmptyState when vault is selected', async () => {
      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
        const state = {
          activeVaultId: 1,
          vaults: [{ id: 1, name: 'My Vault' }],
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

      mockUseMemorySearch.mockReturnValue({
        memories: [],
        searchQuery: '',
        setSearchQuery: vi.fn(),
        loading: false,
        handleSearch: vi.fn(),
      });

      await act(async () => {
        render(<MemoryPage />);
      });

      await waitFor(() => {
        const selectVaultEmptyState = screen.queryByText('Select a vault');
        expect(selectVaultEmptyState).not.toBeInTheDocument();
      });
    });

    it('passes activeVaultId to useMemorySearch hook', async () => {
      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
        const state = {
          activeVaultId: 77,
          vaults: [{ id: 77, name: 'Target Vault' }],
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

      mockUseMemorySearch.mockClear();
      mockUseMemorySearch.mockReturnValue({
        memories: [],
        searchQuery: '',
        setSearchQuery: vi.fn(),
        loading: false,
        handleSearch: vi.fn(),
      });

      await act(async () => {
        render(<MemoryPage />);
      });

      await waitFor(() => {
        expect(mockUseMemorySearch).toHaveBeenCalledWith(77);
      });
    });

    it('renders memory items when memories exist', async () => {
      const mockMemories = [
        { id: '1', content: 'Test memory content', metadata: {} },
        { id: '2', content: 'Another memory', metadata: {} },
      ];

      const { useVaultStore } = await import('@/stores/useVaultStore');
      vi.mocked(useVaultStore).mockImplementation((selector: any) => {
        const state = {
          activeVaultId: 1,
          vaults: [{ id: 1, name: 'My Vault' }],
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

      mockUseMemorySearch.mockReturnValue({
        memories: mockMemories,
        searchQuery: '',
        setSearchQuery: vi.fn(),
        loading: false,
        handleSearch: vi.fn(),
      });

      await act(async () => {
        render(<MemoryPage />);
      });

      await waitFor(() => {
        expect(screen.getByText('Test memory content')).toBeInTheDocument();
        expect(screen.getByText('Another memory')).toBeInTheDocument();
      });
    });
  });
});
