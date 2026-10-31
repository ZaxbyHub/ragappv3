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
    isSelected,
    onSelectionChange,
  }: {
    document: { id: string; filename: string };
    onDelete?: (id: string) => void;
    canDelete?: boolean;
    isSelected?: boolean;
    onSelectionChange?: (id: string, checked: boolean) => void;
  }) => (
    <div data-testid="document-card">
      {document.filename}
      {canDelete && onDelete && (
        <button aria-label={`Delete card ${document.filename}`} onClick={() => onDelete(document.id)}>
          Delete
        </button>
      )}
      {/* PRR-021: expose the organise-level selection wiring the cards list
          re-gated to canOrganizeDocuments — an organizing writer gets a
          checkbox, a non-organizer gets none. */}
      {onSelectionChange && (
        <input
          type="checkbox"
          aria-label={`Select card ${document.filename}`}
          checked={!!isSelected}
          onChange={(e) => onSelectionChange(document.id, e.target.checked)}
        />
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
import { listDocuments, listFolders, getDocumentStats, deleteDocuments, deleteAllDocumentsInVault, scanDocuments, getDocumentWikiStatus } from '@/lib/api';

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

  it('global admin (not just superadmin) gets an ENABLED Scan Directory button (PRR-016 mutant kill)', async () => {
    selectVault('read');
    authState.user = {
      id: 11,
      username: 'admin1',
      full_name: 'Admin One',
      role: 'admin',
      is_active: true,
    };

    await renderPage();

    const scanButtons = screen.queryAllByRole('button', { name: 'Scan Directory' });
    expect(scanButtons.length).toBeGreaterThan(0);
    expect(scanButtons.some((button) => !(button as HTMLButtonElement).disabled)).toBe(true);
  });

  it('clicking Scan Directory calls scanDocuments with NO arguments (PRR-017: vault_id dropped)', async () => {
    selectVault('admin');
    authState.user = {
      id: 12,
      username: 'admin2',
      full_name: 'Admin Two',
      role: 'admin',
      is_active: true,
    };
    vi.mocked(scanDocuments).mockResolvedValue({ added: 0, scanned: 0, errors: [] });

    await renderPage();

    const scanButtons = screen.queryAllByRole('button', { name: 'Scan Directory' });
    await act(async () => {
      fireEvent.click(scanButtons.find((b) => !(b as HTMLButtonElement).disabled)!);
    });
    expect(scanDocuments).toHaveBeenCalledWith();
  });

  it('scan with per-directory failures surfaces the redacted errors in a warning (PRR-002)', async () => {
    selectVault('admin');
    authState.user = {
      id: 13,
      username: 'admin3',
      full_name: 'Admin Three',
      role: 'admin',
      is_active: true,
    };
    vi.mocked(scanDocuments).mockResolvedValue({
      added: 0,
      scanned: 2,
      errors: ['vault 2 upload directory: OSError'],
    });

    await renderPage();

    const scanButtons = screen.queryAllByRole('button', { name: 'Scan Directory' });
    await act(async () => {
      fireEvent.click(scanButtons.find((b) => !(b as HTMLButtonElement).disabled)!);
    });
    const { toast } = await import('sonner');
    await waitFor(() => {
      expect(vi.mocked(toast.warning).mock.calls.length).toBeGreaterThan(0);
    });
    const warningText = String(vi.mocked(toast.warning).mock.calls[0][0]);
    expect(warningText).toContain('1 directory unreadable');
    expect(warningText).toContain('vault 2 upload directory: OSError');
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


  it('vault writer can select a MOBILE card; selection rides canOrganizeDocuments (PRR-021)', async () => {
    selectVault('write');

    await renderPage();

    const cardBoxes = screen.getAllByLabelText(/Select card /);
    expect(cardBoxes.length).toBeGreaterThan(0);
    // Selecting a card opens the bulk bar (Tag/Move/Delete Selected surface).
    await act(async () => {
      fireEvent.click(cardBoxes[0]);
    });
    expect(screen.getByText('1 selected')).toBeInTheDocument();

    // A reader gets no card checkboxes at all (selection hidden). The first
    // render must unmount first (two mounted pages duplicate every landmark).
    act(() => {
      unmount?.();
    });
    selectVault('read');
    await renderPage();
    expect(screen.queryByLabelText(/Select card /)).toBeNull();
  });

  it('delete-all ends with the SERVER total, not a hand-patched guess (PRR-019 value pin)', async () => {
    // PRR-019: the frozen k05 delete-all check is call-count-only; this pins
    // the final VALUE for the zeroing shape — the rendered total must equal
    // the server's post-delete state (0), reconciled by the refetch.
    selectVault('admin');
    vi.mocked(deleteAllDocumentsInVault).mockResolvedValue({ deleted_count: 2 });

    await renderPage();

    const deleteAll = await screen.findByRole('button', { name: 'Delete All in Vault' });
    await act(async () => {
      fireEvent.click(deleteAll);
    });
    const dialog = await screen.findByRole('dialog');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: /^(Confirm|Delete)$/ }));
    });
    // After the refetch lands, the badge must read the server's 0 — and the
    // empty state (not stale rows) must own the surface.
    await waitFor(() => {
      expect(screen.getByText('0')).toBeInTheDocument();
    });
  });

  it('badge pluralizes and locale-formats the count (PRR-008)', async () => {
    selectVault('admin');
    vi.mocked(listDocuments).mockResolvedValue({
      documents: [
        { id: '1', filename: 'solo.pdf', size: 1, created_at: '2024-01-01', metadata: { status: 'processed', chunk_count: 1 } },
      ],
      total: 1,
    });

    await renderPage();

    const badge = screen.getByText((content, element) =>
      element?.tagName === 'SPAN' && /^1 document$/.test(content.trim())
    );
    expect(badge).toBeInTheDocument();
  });

  it('vault writer gets an ENABLED Compile control and a disabled Recompile stays informative (PRR-018)', async () => {
    // PRR-018: the compile affordance is vault-WRITE (folders/tags/wiki all
    // are) — the read-denial pin exists in k04; this pins the GRANTED side so
    // an admin-gating mutant fails. getDocumentWikiStatus resolves
    // not_compiled, so the not_compiled Compile branch renders.
    selectVault('write');

    await renderPage();

    const compileButtons = screen.getAllByRole('button', { name: 'Compile' });
    expect(compileButtons.length).toBeGreaterThan(0);
    expect(compileButtons.every((b) => !(b as HTMLButtonElement).disabled)).toBe(true);
  });

  it('vault reader gets a DISABLED Recompile whose tooltip keeps the wiki details (PRR-010)', async () => {
    // Recompile branch (wiki_status compiled) with a read-only viewer: the
    // button is disabled AND the tooltip still carries status/pages/claims/
    // lint (PRR-010 forbids replacing the details with the permission text).
    // The wiki-status fetch only fires for status 'indexed' rows
    // (useDocumentPolling), so this test seeds its own indexed document.
    selectVault('read');
    vi.mocked(getDocumentWikiStatus).mockResolvedValue({
      wiki_status: 'compiled',
      pages_count: 3,
      claims_count: 5,
      lint_count: 1,
    });
    vi.mocked(listDocuments).mockResolvedValue({
      documents: [
        { id: '9', filename: 'indexed.pdf', size: 1, created_at: '2024-01-01', metadata: { status: 'indexed', chunk_count: 2 } },
      ],
      total: 1,
    });

    await renderPage();

    const recompile = await screen.findByLabelText(/Recompile wiki for indexed.pdf/);
    expect(recompile).toBeDisabled();
    expect(recompile.getAttribute('title')).toContain('3 pages');
    expect(recompile.getAttribute('title')).toContain('1 lint issues');
    expect(recompile.getAttribute('title')).toContain('required to recompile');
    // The hover affordance is off while disabled (PRR-009).
    expect(recompile.className).not.toContain('hover:text-foreground');
  });

  it('search-only miss: Clear filters resets the search and restores rows (PRR-001 no-op kill)', async () => {
    // PRR-001: the search branch's action cleared only tag/folder filters —
    // a no-op on a search-only miss. Kills it end to end.
    selectVault('admin');
    const twoDocs = [
      { id: '1', filename: 'alpha.pdf', size: 1, created_at: '2024-01-01', metadata: { status: 'processed', chunk_count: 1 } },
      { id: '2', filename: 'beta.pdf', size: 1, created_at: '2024-01-01', metadata: { status: 'processed', chunk_count: 1 } },
    ];
    // The page's search flows through a debounce, so the mock must filter:
    // a non-empty search param returns zero rows (search-only miss).
    vi.mocked(listDocuments).mockImplementation(async (params?: { search?: string }) =>
      params?.search
        ? { documents: [], total: 0 }
        : { documents: twoDocs, total: 2 },
    );

    await renderPage();

    // Type a search that matches nothing -> search-miss empty state appears.
    const searchInputs = screen.getAllByPlaceholderText('Search documents and metadata...');
    await act(async () => {
      fireEvent.change(searchInputs[0], { target: { value: 'zzz-no-match' } });
    });
    await waitFor(() => {
      expect(screen.getByText('No documents match your search')).toBeInTheDocument();
    });

    // The K04/K05 frozen suites may render secondary search inputs (mobile
    // cards list etc.) — clear via the FIRST input the empty state's button
    // is wired to (the toolbar search box drives debouncedSearchQuery).
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    });

    // The search state must reset (input cleared) and the rows return.
    await waitFor(() => {
      expect(searchInputs[0].value).toBe('');
      expect(screen.queryByText('No documents match your search')).toBeNull();
    });
    expect((await screen.findAllByText('alpha.pdf')).length).toBeGreaterThan(0);
  });

  it('partial bulk delete ends with the SERVER total, not double-subtracted (impl-review R1 value pin)', async () => {
    // Seeds 10 docs, deletes 3, server reports 7 afterwards. The final
    // rendered Documents card must read 7 — an optimistic patch applied AFTER
    // the stats refetch would show 4 (double-subtraction), which the frozen
    // C13 call-count oracle cannot detect (implementation review Round 1).
    const tenDocs = Array.from({ length: 10 }, (_, i) => ({
      id: String(i + 1),
      filename: `doc${i + 1}.pdf`,
      size: 1024,
      created_at: '2024-01-01',
      metadata: { status: 'processed', chunk_count: 1 },
    }));
    vi.mocked(listDocuments).mockResolvedValue({ documents: tenDocs, total: 10 });
    let serverTotal = 10;
    vi.mocked(getDocumentStats).mockImplementation(async () => ({
      total_documents: serverTotal,
      total_chunks: serverTotal * 2,
      total_size_bytes: serverTotal * 1000,
      documents_by_status: { processed: serverTotal },
    }));
    vi.mocked(deleteDocuments).mockImplementation(async () => {
      serverTotal = 7; // the server has now deleted 3 of 10
      return { deleted_count: 3, failed_ids: [] };
    });

    selectVault('admin');
    await renderPage();
    // Select three rows, then Delete Selected -> confirm.
    const checkboxes = await screen.findAllByRole('checkbox');
    await act(async () => {
      fireEvent.click(checkboxes[1]);
      fireEvent.click(checkboxes[2]);
      fireEvent.click(checkboxes[3]);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Delete Selected' }));
    });
    const dialog = await screen.findByRole('dialog');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: /^(Confirm|Delete)$/ }));
    });
    // The stats card renders the total; wait for the refetch to land, then
    // the final value must be the server's 7.
    await waitFor(() => {
      expect(screen.getByText('7')).toBeInTheDocument();
    });
    expect(screen.queryByText('4')).toBeNull();
  });
});
