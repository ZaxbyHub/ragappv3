import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import MemoryPage from '@/pages/MemoryPage';

// Hoisted mock functions
const mockFetchVaults = vi.hoisted(() => vi.fn());
const mockSetActiveVault = vi.hoisted(() => vi.fn());
const mockUseMemorySearch = vi.hoisted(() => vi.fn());
const mockUseMemoryCrud = vi.hoisted(() => vi.fn());
const mockUpdateMemory = vi.hoisted(() => vi.fn());
const mockPromoteMemoryToWiki = vi.hoisted(() => vi.fn());
const mockBatchMemoryWikiStatus = vi.hoisted(() => vi.fn());
const mockGetCategoryFromMetadata = vi.hoisted(() => vi.fn());

// Mock useVaultStore — active vault selected so MemoryPageContent renders.
vi.mock('@/stores/useVaultStore', () => ({
  useVaultStore: Object.assign(vi.fn((selector) => {
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
  }), {
    getState: vi.fn(() => ({
      activeVaultId: 1,
      vaults: [{ id: 1, name: 'My Vault' }],
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
  getCategoryFromMetadata: mockGetCategoryFromMetadata,
  getTagsFromMetadata: vi.fn(() => []),
  getSourceFromMetadata: vi.fn(() => ''),
  MAX_MEMORY_CONTENT_LENGTH: 10000,
}));

// Mock @/lib/api
vi.mock('@/lib/api', () => ({
  updateMemory: mockUpdateMemory,
  promoteMemoryToWiki: mockPromoteMemoryToWiki,
  getMemoryWikiStatus: vi.fn(),
  batchMemoryWikiStatus: mockBatchMemoryWikiStatus,
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

describe('MemoryPage A04 edit-dialog field clearing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchVaults.mockReset();
    mockFetchVaults.mockResolvedValue(undefined);
    mockSetActiveVault.mockReset();
    mockUpdateMemory.mockReset();
    mockUpdateMemory.mockResolvedValue({ id: '1' });
    mockPromoteMemoryToWiki.mockReset();
    mockBatchMemoryWikiStatus.mockReset();
    mockBatchMemoryWikiStatus.mockResolvedValue({});
    mockGetCategoryFromMetadata.mockReset();
    mockGetCategoryFromMetadata.mockReturnValue('work');

    // Default useMemorySearch return value — one memory with a category.
    mockUseMemorySearch.mockReturnValue({
      memories: [
        { id: '1', content: 'a04 edit target content', metadata: { category: 'work' } },
      ],
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

  it('cleared category is sent as null', async () => {
    await act(async () => {
      render(<MemoryPage />);
    });

    // Open the edit dialog for the memory row.
    fireEvent.click(screen.getByRole('button', { name: 'Edit memory' }));

    const categoryInput = await screen.findByLabelText('Category');
    fireEvent.change(categoryInput, { target: { value: '' } });

    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => {
      expect(mockUpdateMemory).toHaveBeenCalled();
    });

    const payload = mockUpdateMemory.mock.calls[mockUpdateMemory.mock.calls.length - 1][1];
    expect(payload.category).toBe(null);
  });
});
