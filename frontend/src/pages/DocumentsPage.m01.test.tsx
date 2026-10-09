// frontend/src/pages/DocumentsPage.m01.test.tsx
// Issue-trace 781-vaultgate-first-run-baseline — acceptance check C6 (AC1).
//
// The no-selection empty state (DocumentsPage.tsx emptyState ternary, the
// !hasSelectedVault branch, title "Select a vault to view documents") has NO
// action and NO selector: it points at vault-scoping while offering no way to
// pick a vault from where the user stands. The header-level VaultSelector
// (line ~750) exists, so a page-level count would mask the gap — this check
// counts selectors WITHIN the empty-state container only.
//
// Reuses the mock setup of DocumentsPage.test.tsx (same vi.mock set, same
// MemoryRouter wrapper, same useVaultStore mockReturnValue seeding), plus the
// same VaultSelector data-testid stub as MemoryPage.m01.test.tsx.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render as rtlRender, act, waitFor, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router-dom';

// DocumentsPage renders <Link> for document names; provide a router context.
const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: vi.fn(({ count, estimateSize }) => {
    const size = estimateSize?.() ?? 72;
    return {
      getVirtualItems: () =>
        Array.from({ length: count }, (_, i) => ({
          index: i,
          start: i * size,
          size,
          key: `doc-${i}`,
        })),
      getTotalSize: () => count * size,
      measureElement: vi.fn(),
      scrollToIndex: vi.fn(),
      measure: vi.fn(),
    };
  }),
}));

// Mock API with documents so the table renders
vi.mock('@/lib/api', () => ({
  listDocuments: vi.fn().mockResolvedValue({
    documents: [
      { id: '1', filename: 'test.pdf', size: 1024, created_at: '2024-01-01', metadata: { status: 'processed', chunk_count: 5 } },
      { id: '2', filename: 'test2.pdf', size: 2048, created_at: '2024-01-02', metadata: { status: 'processed', chunk_count: 10 } },
    ],
    total: 2,
  }),
  scanDocuments: vi.fn().mockResolvedValue({ added: 0, scanned: 0 }),
  deleteDocument: vi.fn().mockResolvedValue({}),
  deleteDocuments: vi.fn().mockResolvedValue({ deleted_count: 0, failed_ids: [] }),
  deleteAllDocumentsInVault: vi.fn().mockResolvedValue({ deleted_count: 0 }),
  getDocumentWikiStatus: vi.fn().mockResolvedValue({
    wiki_status: 'not_compiled',
    pages_count: 0,
    claims_count: 0,
    lint_count: 0,
  }),
  compileDocumentWiki: vi.fn().mockResolvedValue({ job_id: 1, status: 'queued' }),
  getDocumentStats: vi.fn().mockResolvedValue({
    total_documents: 2,
    total_chunks: 15,
    total_size_bytes: 3072,
    documents_by_status: { processed: 2 },
  }),
  listTags: vi.fn().mockResolvedValue([]),
  listFolders: vi.fn().mockResolvedValue([]),
  createFolder: vi.fn().mockResolvedValue({ id: 1, name: 'mock', vault_id: 1, parent_folder_id: null }),
  updateFolder: vi.fn().mockResolvedValue({ id: 1, name: 'updated', vault_id: 1, parent_folder_id: null }),
  deleteFolder: vi.fn().mockResolvedValue(undefined),
  downloadDocument: vi.fn().mockResolvedValue(undefined),
}));

// Mock react-dropzone
vi.mock('react-dropzone', () => ({
  useDropzone: vi.fn(() => ({
    getRootProps: () => ({ role: 'button' }),
    getInputProps: () => ({ type: 'file' }),
    isDragActive: false,
  })),
}));

// Mock sonner toast
vi.mock('sonner', () => {
  const toast = Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
    dismiss: vi.fn(),
  });
  return { toast };
});

// Mock useDebounce hook
vi.mock('@/hooks/useDebounce', () => ({
  useDebounce: vi.fn((value: string) => [value, false]),
}));

// Mock useVaultStore
vi.mock('@/stores/useVaultStore', () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: null,
    vaults: [],
  })),
}));

// Mock useUploadStore (getState: the mounted useUploadMonitoring hook reads it)
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

// Mock UI components
vi.mock('@/components/ui/card', () => ({
  Card: ({ children }: { children: React.ReactNode }) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: { children: React.ReactNode }) => <div data-testid="card-content">{children}</div>,
  CardDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  CardHeader: ({ children }: { children: React.ReactNode }) => <div data-testid="card-header">{children}</div>,
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

vi.mock('@/components/ui/progress', () => ({
  Progress: () => <div role="progressbar" />,
}));

vi.mock('@/components/ui/skeleton', () => ({
  Skeleton: () => <div data-testid="skeleton" />,
}));

vi.mock('@/components/ui/checkbox', () => ({
  Checkbox: ({ onCheckedChange, checked, ...props }: { onCheckedChange?: (checked: boolean) => void; checked?: boolean }) => (
    <input type="checkbox" onChange={(e) => onCheckedChange?.(e.target.checked)} checked={checked} {...props} />
  ),
}));

// Same data-testid stub as MemoryPage.m01.test.tsx — counts the selector
// wherever it is rendered (header row AND, post-fix, the empty state).
vi.mock('@/components/vault/VaultSelector', () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock('@/components/shared/StatusBadge', () => ({
  StatusBadge: ({ status, chunksFailed }: { status: string; chunksFailed?: number }) => (
    <span data-testid="status-badge">
      {status === 'indexed' && (chunksFailed ?? 0) > 0 ? 'Partially indexed' : status}
    </span>
  ),
}));

vi.mock('@/components/shared/DocumentCard', () => ({
  DocumentCard: ({ document }: { document: { id: string; filename: string } }) => (
    <div data-testid="document-card">{document.filename}</div>
  ),
}));

// AMENDED (CHECK_WRONG, sanctioned pre-implementation): the original mock
// modelled `action` as the object form only, but the real EmptyState contract
// is `action?: ReactNode | EmptyStateAction` (EmptyState.tsx isActionObject) —
// as first frozen, no implementation could render a control through the
// action prop. The mock now mirrors the real component's discrimination.
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

// Import component after mocks
import DocumentsPage from '@/pages/DocumentsPage';
import { useVaultStore } from '@/stores/useVaultStore';
import { getDocumentStats, listDocuments } from '@/lib/api';

describe('DocumentsPage m01 (issue-trace 781-vaultgate-first-run-baseline)', () => {
  let unmount: (() => void) | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listDocuments).mockResolvedValue({ documents: [], total: 0 });
    vi.mocked(getDocumentStats).mockResolvedValue({
      total_documents: 0,
      total_chunks: 0,
      total_size_bytes: 0,
      documents_by_status: {},
    });
    vi.mocked(window.localStorage.getItem).mockReturnValue(null);
  });

  afterEach(() => {
    if (unmount) {
      unmount();
    }
    unmount = undefined;
    vi.restoreAllMocks();
  });

  it('no-selection empty state offers the vault selector', async () => {
    // First-run scenario: one accessible vault exists, none is selected.
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: null,
      vaults: [{ id: 2, name: 'Team Vault', current_user_permission: 'write' }],
    } as ReturnType<typeof useVaultStore>);

    await act(async () => {
      const result = render(<DocumentsPage />);
      unmount = result.unmount;
    });

    await waitFor(() => {
      expect(screen.getByText('Select a vault to view documents')).toBeInTheDocument();
    });

    // Count selectors INSIDE the no-selection empty state only — the header
    // row already renders one, so a page-level count would mask the gap.
    const emptyState = screen
      .getByText('Select a vault to view documents')
      .closest('div[data-testid="empty-state"]');
    expect(emptyState).not.toBeNull();
    expect(within(emptyState as HTMLElement).queryAllByTestId('vault-selector').length).toBe(1);
  });
});
