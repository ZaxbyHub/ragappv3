// frontend/src/tests/vaultGateSites.781.guardrail.test.tsx
// Issue #781 (Phase 4.2 guardrail, defect class C17): every vault-gated
// null state IN SCOPE of this PR must render the control its copy points at
// — the shared VaultGate (selector + open-vaults action), click-triggered
// navigation only. This is the class guardrail on top of the frozen
// per-surface checks: it pins that the Memory site specifically uses
// VaultGate (AC5 site coverage, which no frozen check pins) and that the
// gate never navigates on mount.
//
// Mock conventions mirror the frozen m01 fixtures (DocumentsPage.m01 /
// MemoryPage.m01 superset) plus a useNavigate spy for the mount assertion.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render as rtlRender, act, waitFor, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router-dom';

const mockNavigate = vi.fn();

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: vi.fn(({ count, estimateSize }) => {
    const size = estimateSize?.() ?? 72;
    return {
      getVirtualItems: () =>
        Array.from({ length: count }, (_, i) => ({ index: i, start: i * size, size, key: `v-${i}` })),
      getTotalSize: () => count * size,
      measureElement: vi.fn(),
      scrollToIndex: vi.fn(),
      measure: vi.fn(),
    };
  }),
}));

vi.mock('@/lib/api', () => ({
  // Issue #782 additions: KMS + Wiki page surfaces (the two new gate sites).
  listKMSEntries: vi.fn().mockResolvedValue({ entries: [], total: 0, page: 1, per_page: 200 }),
  recompileVaultKMS: vi.fn().mockResolvedValue({ job_id: 1, status: 'pending' }),
  listWikiPages: vi.fn().mockResolvedValue({ pages: [], page: 1, per_page: 50 }),
  listWikiLintFindings: vi.fn().mockResolvedValue({ findings: [] }),
  getWikiActivityFeed: vi.fn().mockResolvedValue([]),
  API_BASE_URL: '/api',
  getJwtAccessToken: vi.fn(() => null),
  refreshAccessToken: vi.fn(),
  listDocuments: vi.fn().mockResolvedValue({ documents: [], total: 0 }),
  getDocumentStats: vi.fn().mockResolvedValue({
    total_documents: 0,
    total_chunks: 0,
    total_size_bytes: 0,
    documents_by_status: {},
  }),
  scanDocuments: vi.fn().mockResolvedValue({ added: 0, scanned: 0 }),
  deleteDocument: vi.fn().mockResolvedValue({}),
  deleteDocuments: vi.fn().mockResolvedValue({ deleted_count: 0, failed_ids: [] }),
  deleteAllDocumentsInVault: vi.fn().mockResolvedValue({ deleted_count: 0 }),
  getDocumentWikiStatus: vi.fn().mockResolvedValue({ wiki_status: 'not_compiled', pages_count: 0, claims_count: 0, lint_count: 0 }),
  compileDocumentWiki: vi.fn().mockResolvedValue({ job_id: 1, status: 'queued' }),
  listTags: vi.fn().mockResolvedValue([]),
  listFolders: vi.fn().mockResolvedValue([]),
  createFolder: vi.fn().mockResolvedValue({ id: 1, name: 'm', vault_id: 1, parent_folder_id: null }),
  updateFolder: vi.fn().mockResolvedValue({ id: 1, name: 'u', vault_id: 1, parent_folder_id: null }),
  deleteFolder: vi.fn().mockResolvedValue(undefined),
  downloadDocument: vi.fn().mockResolvedValue(undefined),
  // MemoryPage surface
  updateMemory: vi.fn(),
  promoteMemoryToWiki: vi.fn(),
  getMemoryWikiStatus: vi.fn(),
  batchMemoryWikiStatus: vi.fn().mockResolvedValue({}),
}));

const mockUseMemorySearch = vi.hoisted(() => vi.fn());
const mockUseMemoryCrud = vi.hoisted(() => vi.fn());

vi.mock('@/hooks/useMemorySearch', () => ({ useMemorySearch: mockUseMemorySearch }));
vi.mock('@/hooks/useMemoryCrud', () => ({
  useMemoryCrud: mockUseMemoryCrud,
  getCategoryFromMetadata: vi.fn(() => 'Uncategorized'),
  getTagsFromMetadata: vi.fn(() => []),
  getSourceFromMetadata: vi.fn(() => ''),
  MAX_MEMORY_CONTENT_LENGTH: 10000,
}));

vi.mock('react-dropzone', () => ({
  useDropzone: vi.fn(() => ({
    getRootProps: () => ({ role: 'button' }),
    getInputProps: () => ({ type: 'file' }),
    isDragActive: false,
  })),
}));

vi.mock('sonner', () => {
  const toast = Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), dismiss: vi.fn() });
  return { toast };
});

vi.mock('@/hooks/useDebounce', () => ({
  useDebounce: vi.fn((value: string) => [value, false]),
}));

vi.mock('@/stores/useVaultStore', () => ({
  useVaultStore: vi.fn(() => ({ activeVaultId: null, vaults: [] })),
}));

vi.mock('@/stores/useUploadStore', () => ({
  uploadNeedsMonitoring: () => false,
  useUploadStore: Object.assign(
    vi.fn(() => ({
      uploads: [],
      addUploads: vi.fn(),
      cancelUpload: vi.fn(),
      removeUpload: vi.fn(),
      clearCompleted: vi.fn(),
      retryUpload: vi.fn(),
    })),
    { getState: () => ({ uploads: [] }) }
  ),
}));

vi.mock('@/components/ui/card', () => ({
  Card: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  CardContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  CardDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  CardHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h3>{children}</h3>,
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

vi.mock('@/components/ui/progress', () => ({ Progress: () => <div role="progressbar" /> }));
vi.mock('@/components/ui/skeleton', () => ({ Skeleton: () => <div data-testid="skeleton" /> }));

vi.mock('@/components/ui/checkbox', () => ({
  Checkbox: ({ onCheckedChange, checked, ...props }: { onCheckedChange?: (checked: boolean) => void; checked?: boolean }) => (
    <input type="checkbox" onChange={(e) => onCheckedChange?.(e.target.checked)} checked={checked} {...props} />
  ),
}));

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ open, children }: { open?: boolean; children: React.ReactNode }) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

vi.mock('@/components/ui/textarea', () => ({
  Textarea: (props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} />,
}));

vi.mock('@/components/ui/label', () => ({
  Label: ({ children, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) => <label {...props}>{children}</label>,
}));

// VaultSelector stub — VaultGate itself is REAL (the point of this guardrail:
// the in-scope sites must use the shared component, not a re-implementation).
vi.mock('@/components/vault/VaultSelector', () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock('@/components/shared/StatusBadge', () => ({
  StatusBadge: ({ status }: { status: string }) => <span data-testid="status-badge">{status}</span>,
}));

vi.mock('@/components/shared/DocumentCard', () => ({
  DocumentCard: ({ document }: { document: { id: string; filename: string } }) => (
    <div data-testid="document-card">{document.filename}</div>
  ),
}));

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
      <div data-testid="empty-state">
        <h2>{title}</h2>
        {description && <p>{description}</p>}
        {action && (isActionObject(action) ? <button onClick={action.onClick}>{action.label}</button> : action)}
      </div>
    );
  },
}));

vi.mock('@/lib/formatters', () => ({
  formatFileSize: (bytes: number) => `${bytes} bytes`,
  formatDate: (date: string) => date,
}));

vi.mock('@/components/documents/UploadDropzone', () => ({
  UploadDropzone: () => <div data-testid="upload-dropzone-stub" />,
}));

vi.mock('@/components/documents/RejectedFilesBanner', () => ({
  RejectedFilesBanner: () => null,
}));

// Issue #782: Wiki child components mocked so the parent's null branch is
// isolated (same isolation shape as WikiPage.test.tsx).
vi.mock('@/pages/WikiPageList', () => ({
  WikiPageList: () => <div data-testid="wiki-page-list">Page List</div>,
  PAGE_TYPES: [{ value: '', label: 'All' }],
}));
vi.mock('@/pages/WikiPageDetail', () => ({
  WikiPageDetail: () => <div data-testid="wiki-page-detail" />,
}));
vi.mock('@/pages/WikiEditDialog', () => ({
  WikiEditDialog: () => null,
}));
vi.mock('@/pages/WikiLintPanel', () => ({
  WikiLintPanel: () => <div data-testid="wiki-lint-panel" />,
}));

vi.mock('@/components/layout/PageTitleHeader', () => ({
  PageTitleHeader: ({ title }: { title: string }) => <div data-testid="page-title">{title}</div>,
}));


import MemoryPage from '@/pages/MemoryPage';
import DocumentsPage from '@/pages/DocumentsPage';
import KMSPage from '@/pages/KMSPage';
import WikiPage from '@/pages/WikiPage';
import { useVaultStore } from '@/stores/useVaultStore';
import { listDocuments, getDocumentStats } from '@/lib/api';

function seedVaultSelection(activeVaultId: number | null, vaults: Array<{ id: number; name: string }>) {
  vi.mocked(useVaultStore).mockReturnValue({ activeVaultId, vaults } as ReturnType<typeof useVaultStore>);
}

describe('vault-gate sites guardrail (issue #781, class C17)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockNavigate.mockClear();
    // WikiPage mounts useWikiEventStream: stub fetch with an open
    // (never-resolving) stream so no real request is made (WikiPage.test
    // beforeEach pattern).
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          body: {
            getReader: () => ({
              read: () => new Promise(() => {}),
              cancel: vi.fn(),
            }),
          },
        } as unknown as Response)
      )
    );
    vi.mocked(listDocuments).mockResolvedValue({ documents: [], total: 0 });
    vi.mocked(getDocumentStats).mockResolvedValue({
      total_documents: 0,
      total_chunks: 0,
      total_size_bytes: 0,
      documents_by_status: {},
    });
    mockUseMemorySearch.mockReturnValue({
      memories: [],
      searchQuery: '',
      setSearchQuery: vi.fn(),
      loading: false,
      handleSearch: vi.fn(),
    });
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
    vi.unstubAllGlobals();
  });

  it('Memory null branch renders the shared VaultGate (selector + action, click-only navigation)', async () => {
    seedVaultSelection(null, [{ id: 42, name: 'Accessible Vault' }]);

    await act(async () => {
      render(<MemoryPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Select a vault')).toBeInTheDocument();
    });

    // The title is positively "Memory" (feedback round FB-002: the frozen
    // C3 row only pins the absence of "Memories" — a deleted title would
    // slip past it; this assertion closes that shape).
    expect(screen.getByTestId('page-title')).toHaveTextContent('Memory');

    // The gate's own control set: exactly the selector VaultGate renders...
    expect(screen.queryAllByTestId('vault-selector').length).toBe(1);
    // ...plus VaultGate's open-vaults action (real VaultGate in play)...
    expect(screen.getByRole('button', { name: 'Open Vaults' })).toBeInTheDocument();
    // ...and the page's memory surface stays unmounted behind the gate.
    expect(screen.queryByText('Add Memory')).not.toBeInTheDocument();

    // Navigation is click-triggered only — mounting the gate must never
    // redirect the user away from the page they were reading.
    expect(mockNavigate).not.toHaveBeenCalled();
    // MemoryPageContent (and its queries) still never mounts on this branch.
    expect(mockUseMemorySearch).not.toHaveBeenCalled();
  });

  it('Documents no-selection empty state offers the gate selector inline', async () => {
    seedVaultSelection(null, [{ id: 2, name: 'Team Vault' }]);

    await act(async () => {
      render(<DocumentsPage />);
    });

    await waitFor(() => {
      expect(screen.getByText('Select a vault to view documents')).toBeInTheDocument();
    });

    const emptyState = screen
      .getByText('Select a vault to view documents')
      .closest('div[data-testid="empty-state"]');
    expect(emptyState).not.toBeNull();
    expect(within(emptyState as HTMLElement).queryAllByTestId('vault-selector').length).toBe(1);
  });

  // Issue #782 (class C17 continuation): the two PR-2 gate sites. Assertions
  // run against THIS file's own mocks (mocked useNavigate never throws;
  // mocked EmptyState exposes data-testid="empty-state") — the bare-render
  // router-crash class is pinned by VaultGate.m02.test.tsx with the real
  // router, and the frozen C1/C2 checks pin the real DOM scoping.
  it('KMS no-selection state offers the gate selector inline and does not navigate on mount', async () => {
    seedVaultSelection(null, [{ id: 7, name: 'Team Vault' }]);

    await act(async () => {
      render(<KMSPage />);
    });

    const emptyState = screen
      .getByText('Select a vault to view its knowledge entries.')
      .closest('div[data-testid="empty-state"]');
    expect(emptyState).not.toBeNull();
    expect(within(emptyState as HTMLElement).queryAllByTestId('vault-selector').length).toBe(1);
    expect(within(emptyState as HTMLElement).getByText('Open Vaults')).toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('Wiki no-selection empty state offers the gate selector inline and does not navigate on mount', async () => {
    seedVaultSelection(null, [{ id: 7, name: 'Team Vault' }]);

    await act(async () => {
      render(<WikiPage />);
    });

    const emptyState = screen
      .getByText('Select a vault')
      .closest('div[data-testid="empty-state"]');
    expect(emptyState).not.toBeNull();
    expect(within(emptyState as HTMLElement).queryAllByTestId('vault-selector').length).toBe(1);
    expect(within(emptyState as HTMLElement).getByText('Open Vaults')).toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
