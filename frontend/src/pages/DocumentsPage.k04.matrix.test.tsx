/**
 * DocumentsPage K04 permission MATRIX (issue #767): the client gates must
 * mirror the server's capability split across role combinations.
 *
 *   writer  (vault write)  — organize surface (selection, bulk tag/move,
 *                            folders) WITHOUT any destructive arm;
 *   admin   (vault admin)  — destructive arms (per-row delete, Delete
 *                            Selected, Delete All in Vault);
 *   global superadmin      — Scan Directory ENABLED (global-admin route);
 *   global viewer          — Scan Directory disabled;
 *   ConfirmDialog default  — the FOLDER delete dialog's destructive button
 *                            stays "Confirm" (only the single-document
 *                            dialog overrides it to "Delete");
 *   filter empty state     — a folder-filter miss in a populated vault is
 *                            "No documents match your filters" with a
 *                            working "Clear filters" action, while an
 *                            active search keeps the search wording;
 *   mobile cards           — DocumentCardsList delete opens the dialog that
 *                            names the document (filename pass-through).
 *
 * Harness cloned from DocumentsPage.k04.test.tsx (proven in this repo).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render as rtlRender, fireEvent, act, waitFor, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router-dom';

// DocumentsPage renders <Link> for document names; provide a router context.
const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}

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

const ROWS = {
  documents: [
    { id: '1', filename: 'test.pdf', size: 1024, created_at: '2024-01-01', metadata: { status: 'processed', chunk_count: 5 } },
    { id: '2', filename: 'test2.pdf', size: 2048, created_at: '2024-01-02', metadata: { status: 'processed', chunk_count: 10 } },
  ],
  total: 2,
};

// Mock API with documents so the table renders. listDocuments is
// re-implemented in beforeEach so per-test overrides never leak.
vi.mock('@/lib/api', () => ({
  listDocuments: vi.fn(),
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
  listFolders: vi.fn(),
  createFolder: vi.fn().mockResolvedValue({ id: 1, name: 'mock', vault_id: 1, parent_folder_id: null }),
  updateFolder: vi.fn().mockResolvedValue({ id: 1, name: 'updated', vault_id: 1, parent_folder_id: null }),
  deleteFolder: vi.fn().mockResolvedValue(undefined),
  downloadDocument: vi.fn().mockResolvedValue(undefined),
}));

// Mock sonner toast. `toast` is callable with method shortcuts.
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

// Mock useVaultStore (per-test override via vi.mocked(useVaultStore))
vi.mock('@/stores/useVaultStore', () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: null,
    vaults: [],
  })),
}));

// Mock useAuthStore for the global-role gate. The factory honours
// zustand-style selectors so the component's read works either way.
const { authState } = vi.hoisted(() => ({
  authState: {
    user: null as {
      id: number;
      username: string;
      full_name: string;
      role: string;
      is_active: boolean;
    } | null,
  },
}));
vi.mock('@/stores/useAuthStore', () => ({
  useAuthStore: vi.fn(
    (selector?: (state: typeof authState) => unknown) =>
      selector ? selector(authState) : authState
  ),
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
  CardDescription: ({ children }: { children: React.ReactNode }) => <p data-testid="card-description">{children}</p>,
  CardHeader: ({ children }: { children: React.ReactNode }) => <div data-testid="card-header">{children}</div>,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h3 data-testid="card-title">{children}</h3>,
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

// DocumentCard stub that EXPOSES the delete + selection wiring the mobile
// DocumentCardsList passes through (canDelete = admin-level capability).
vi.mock('@/components/shared/DocumentCard', () => ({
  DocumentCard: ({
    document,
    onDelete,
    canDelete,
  }: {
    document: { id: string; filename: string };
    onDelete?: (id: string) => void;
    canDelete?: boolean;
  }) => (
    <div data-testid="document-card">
      {document.filename}
      {canDelete && onDelete && (
        <button aria-label={`Delete card ${document.filename}`} onClick={() => onDelete(document.id)}>
          Delete
        </button>
      )}
    </div>
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
    action?: { label: string; onClick: () => void };
  }) => (
    <div data-testid="empty-state">
      <h2>{title}</h2>
      {description && <p>{description}</p>}
      {action && <button onClick={action.onClick}>{action.label}</button>}
    </div>
  ),
}));

vi.mock('@/lib/formatters', () => ({
  formatFileSize: (bytes: number) => `${bytes} bytes`,
  formatDate: (date: string) => date,
}));

vi.mock('@/components/documents/UploadDropzone', () => ({
  UploadDropzone: () => <div data-testid="upload-dropzone-stub" />,
}));

// Import component after mocks
import DocumentsPage from '@/pages/DocumentsPage';
import { useVaultStore } from '@/stores/useVaultStore';
import { listDocuments, listFolders } from '@/lib/api';

interface ListDocumentsParams {
  folderId?: number;
  search?: string;
}

async function flushMicrotasks(rounds = 12) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

function selectVault(permission: 'write' | 'admin' | 'read') {
  vi.mocked(useVaultStore).mockReturnValue({
    activeVaultId: 2,
    vaults: [{ id: 2, name: `Vault (${permission})`, current_user_permission: permission }],
  } as ReturnType<typeof useVaultStore>);
}

describe('DocumentsPage K04 permission matrix (issue #767)', () => {
  let container: HTMLElement;
  let unmount: (() => void) | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    authState.user = null;
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: null,
      vaults: [],
    } as ReturnType<typeof useVaultStore>);
    vi.mocked(listDocuments).mockImplementation(async (params?: ListDocumentsParams) =>
      params && (params.folderId != null || params.search)
        ? { documents: [], total: 0 }
        : ROWS
    );
    vi.mocked(listFolders).mockResolvedValue([]);
  });

  afterEach(() => {
    if (unmount) {
      unmount();
    }
    unmount = undefined;
    vi.restoreAllMocks();
  });

  async function renderPage() {
    let result;
    await act(async () => {
      result = render(<DocumentsPage />);
    });
    container = result.container;
    unmount = result.unmount;
    await waitFor(() => {
      expect(screen.getByText('Folders')).toBeInTheDocument();
    });
    await act(async () => {
      await flushMicrotasks();
    });
    return result;
  }

  it('vault writer gets the organize surface but no destructive arms (no row delete, no Delete Selected, no Delete All)', async () => {
    selectVault('write');

    await renderPage();

    // Rows render (the name appears in both the table and the cards list);
    // per-row delete is admin-level and must be absent.
    expect((await screen.findAllByText('test.pdf')).length).toBeGreaterThan(0);
    expect(screen.queryAllByLabelText('Delete document')).toHaveLength(0);

    // Writer CAN select rows (bulk tag/move are vault-write server-side)...
    const rowCheckbox = screen.getByLabelText('Select test.pdf') as HTMLInputElement;
    await act(async () => {
      fireEvent.click(rowCheckbox);
    });
    expect(screen.getByText('1 selected')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Tag' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Move' })).toBeInTheDocument();

    // ...but the destructive bulk arm stays absent, and so does Delete All.
    expect(screen.queryByRole('button', { name: 'Delete Selected' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete All in Vault' })).toBeNull();
  });

  it('vault admin gets the destructive arms (Delete All in Vault, Delete Selected once rows are selected)', async () => {
    selectVault('admin');

    await renderPage();

    expect((await screen.findAllByText('test.pdf')).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Delete All in Vault' })).toBeInTheDocument();

    // Delete Selected appears once a row is selected, enabled.
    expect(screen.queryByRole('button', { name: 'Delete Selected' })).toBeNull();
    const rowCheckbox = screen.getByLabelText('Select test.pdf') as HTMLInputElement;
    await act(async () => {
      fireEvent.click(rowCheckbox);
    });
    const deleteSelected = screen.getByRole('button', { name: 'Delete Selected' }) as HTMLButtonElement;
    expect(deleteSelected).toBeEnabled();
  });

  it('global superadmin gets an ENABLED Scan Directory button', async () => {
    selectVault('read');
    authState.user = {
      id: 9,
      username: 'root1',
      full_name: 'Root One',
      role: 'superadmin',
      is_active: true,
    };

    await renderPage();

    const scanButtons = screen.queryAllByRole('button', { name: 'Scan Directory' });
    expect(scanButtons.length).toBeGreaterThan(0);
    expect(scanButtons.some((button) => !(button as HTMLButtonElement).disabled)).toBe(true);
  });

  it('global viewer gets a DISABLED Scan Directory button even with vault access', async () => {
    selectVault('admin');
    authState.user = {
      id: 10,
      username: 'viewer1',
      full_name: 'Viewer One',
      role: 'viewer',
      is_active: true,
    };

    await renderPage();

    const scanButtons = screen.queryAllByRole('button', { name: 'Scan Directory' });
    expect(scanButtons.length).toBeGreaterThan(0);
    expect(scanButtons.some((button) => !(button as HTMLButtonElement).disabled)).toBe(false);
  });

  it('folder delete dialog keeps the default destructive label "Confirm" (not "Delete")', async () => {
    selectVault('write');
    vi.mocked(listFolders).mockResolvedValue([
      { id: 5, name: 'Taxes', vault_id: 2, parent_folder_id: null, document_count: 0 },
    ]);

    await renderPage();

    // Open the folder's action menu (keyboard: radix trigger opens on Enter).
    const trigger = screen.getByLabelText('Folder actions for Taxes');
    await act(async () => {
      fireEvent.keyDown(trigger, { key: 'Enter' });
    });
    const menuItems = await screen.findAllByRole('menuitem');
    const deleteItem = menuItems.find((item) => item.textContent === 'Delete');
    expect(deleteItem).toBeTruthy();
    await act(async () => {
      fireEvent.keyDown(deleteItem as HTMLElement, { key: 'Enter' });
    });

    // The shared ConfirmDialog opens for the FOLDER delete; its destructive
    // button keeps the "Confirm" default (only the document dialog says "Delete").
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Delete Folder')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Confirm' })).toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Delete' })).toBeNull();
  });

  it('folder filter with zero matches in a populated vault shows the filter empty state and Clear filters restores the list', async () => {
    selectVault('write');
    vi.mocked(listFolders).mockResolvedValue([
      { id: 5, name: 'Taxes', vault_id: 2, parent_folder_id: null, document_count: 0 },
    ]);

    await renderPage();

    // Select the folder: the (mocked) server returns zero rows for it, but
    // stats say the vault is populated — a filter miss, not an empty vault.
    const folderButton = screen.getByRole('button', { name: /^Taxes/ });
    await act(async () => {
      fireEvent.click(folderButton);
    });
    expect(await screen.findByText('No documents match your filters')).toBeInTheDocument();
    expect(screen.getByText('The selected tag or folder does not match any document in this vault.')).toBeInTheDocument();

    // Clear filters resets the folder filter; the unfiltered refetch returns rows.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    });
    await waitFor(() => {
      expect(screen.queryByText('No documents match your filters')).toBeNull();
    });
    expect((await screen.findAllByText('test.pdf')).length).toBeGreaterThan(0);
  });

  it('combined search + folder filter miss keeps the search empty-state wording', async () => {
    selectVault('write');
    vi.mocked(listFolders).mockResolvedValue([
      { id: 5, name: 'Taxes', vault_id: 2, parent_folder_id: null, document_count: 0 },
    ]);

    await renderPage();

    const search = screen.getByPlaceholderText('Search documents and metadata...');
    await act(async () => {
      fireEvent.change(search, { target: { value: 'zzz' } });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^Taxes/ }));
    });

    expect(await screen.findByText('No documents match your search')).toBeInTheDocument();
    expect(screen.queryByText('No documents match your filters')).toBeNull();
  });

  it('mobile cards delete opens the document dialog naming the file, with destructive label "Delete"', async () => {
    selectVault('admin');
    vi.mocked(listDocuments).mockResolvedValue({
      documents: [
        { id: '7', filename: 'mobile.pdf', size: 512, created_at: '2024-02-03', metadata: { status: 'processed', chunk_count: 1 } },
      ],
      total: 1,
    });

    await renderPage();

    // DocumentCardsList (the sm:hidden mobile list) passes the FILENAME
    // through onDelete; canDelete follows the admin-level capability.
    const cardDelete = await screen.findByLabelText('Delete card mobile.pdf');
    await act(async () => {
      fireEvent.click(cardDelete);
    });

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Delete Document')).toBeInTheDocument();
    expect(within(dialog).getByText(/"mobile\.pdf"/)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });
});
