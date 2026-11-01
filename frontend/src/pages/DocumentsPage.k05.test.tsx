import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render as rtlRender, fireEvent, act, waitFor, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router-dom';

// DocumentsPage renders <Link> for document names; provide a router context.
const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

// #767 AC5 (CHECK_WRONG amend, sanctioned): DocumentsPage now reads the
// user's global role for the scan gate, so the real useAuthStore (module-level
// attachCsrfInterceptor side effects, wide import graph) must not load here.
const { authState } = vi.hoisted(() => ({
  authState: {
    user: { id: 1, username: "admin1", full_name: "Admin One", role: "admin", is_active: true },
  },
}));
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(
    (selector?: (state: typeof authState) => unknown) =>
      selector ? selector(authState) : authState
  ),
}));

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

// Mock API with documents so the table renders. Harness cloned from
// DocumentsPage.test.tsx (proven in this repo).
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
import {
  deleteAllDocumentsInVault,
  deleteDocuments,
  getDocumentStats,
  listDocuments,
  listFolders,
  type Folder,
} from '@/lib/api';

async function flushMicrotasks(rounds = 12) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

const ADMIN_VAULT_STORE = {
  activeVaultId: 2,
  vaults: [{ id: 2, name: 'Admin Vault', current_user_permission: 'admin' }],
} as ReturnType<typeof useVaultStore>;

describe('DocumentsPage K05 truthfulness checks (issue #768)', () => {
  let container: HTMLElement;
  let unmount: (() => void) | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useVaultStore).mockReturnValue(ADMIN_VAULT_STORE);
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

  // Finds the dialog's destructive button without depending on whether the
  // label reads Confirm or Delete (only the single-delete dialog renames).
  async function findDialogDestructiveButton() {
    const dialog = await screen.findByRole('dialog');
    const destructive = await within(dialog).findAllByRole('button', {
      name: /^(Confirm|Delete)$/,
    });
    return { dialog, destructive: destructive[0] };
  }

  it('header count badge shows the matching total', async () => {
    // 50 rows fetched, 120 matching on the server. RED at base (the badge
    // counts the fetched window), GREEN after the fix.
    const fiftyDocs = Array.from({ length: 50 }, (_, i) => ({
      id: String(i + 1),
      filename: `doc-${i + 1}.pdf`,
      size: 1024,
      created_at: '2024-01-01',
      metadata: { status: 'processed', chunk_count: 1 },
    }));
    vi.mocked(listDocuments).mockResolvedValue({ documents: fiftyDocs, total: 120 });

    await renderPage();

    // The toolbar badge's whole text is "<n> documents" — the pagination
    // line ("Showing 50 of 120 documents") must not match.
    const badgeSpans = Array.from(container.querySelectorAll('span')).filter((el) =>
      /^\d[\d,]* documents$/.test((el.textContent ?? '').trim())
    );
    expect(badgeSpans.length).toBeGreaterThan(0);
    const shown = Number(
      badgeSpans[0].textContent?.replace(/ documents$/, '').replace(/,/g, '')
    );
    expect(shown).toBe(120);
  });

  it('bulk delete refetches document stats', async () => {
    vi.mocked(deleteDocuments).mockResolvedValueOnce({ deleted_count: 2, failed_ids: [] });

    await renderPage();

    const selectAll = container.querySelector(
      'input[aria-label="Select all documents"]'
    ) as HTMLInputElement;
    await act(async () => {
      fireEvent.click(selectAll);
    });
    const deleteSelected = await screen.findByRole('button', { name: 'Delete Selected' });
    await act(async () => {
      fireEvent.click(deleteSelected);
    });

    const { destructive } = await findDialogDestructiveButton();
    const before = vi.mocked(getDocumentStats).mock.calls.length;
    await act(async () => {
      fireEvent.click(destructive);
    });
    await waitFor(() => {
      expect(vi.mocked(deleteDocuments)).toHaveBeenCalledTimes(1);
    });
    await act(async () => {
      await flushMicrotasks(25);
    });

    // A successful bulk delete must refetch the server stats. RED at base
    // (only the local total is patched), GREEN after the fix.
    const after = vi.mocked(getDocumentStats).mock.calls.length;
    expect(after - before).toBeGreaterThan(0);
  });

  it('delete all in vault refetches document stats', async () => {
    vi.mocked(deleteAllDocumentsInVault).mockResolvedValueOnce({ deleted_count: 2 });

    await renderPage();

    const deleteAll = await screen.findByRole('button', { name: 'Delete All in Vault' });
    await act(async () => {
      fireEvent.click(deleteAll);
    });

    const { destructive } = await findDialogDestructiveButton();
    const before = vi.mocked(getDocumentStats).mock.calls.length;
    await act(async () => {
      fireEvent.click(destructive);
    });
    await waitFor(() => {
      expect(vi.mocked(deleteAllDocumentsInVault)).toHaveBeenCalledTimes(1);
    });
    await act(async () => {
      await flushMicrotasks(25);
    });

    // A successful delete-all must refetch the server stats. RED at base
    // (only the local total is zeroed), GREEN after the fix.
    const after = vi.mocked(getDocumentStats).mock.calls.length;
    expect(after - before).toBeGreaterThan(0);
  });

  it('empty folder filter in a populated vault does not say no documents yet', async () => {
    const emptyFolder = {
      id: 9,
      vault_id: 2,
      parent_folder_id: null,
      name: 'Empty Folder',
      description: '',
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
      document_count: 0,
    } as Folder;
    vi.mocked(listFolders).mockResolvedValue([emptyFolder]);
    vi.mocked(getDocumentStats).mockResolvedValue({
      total_documents: 5,
      total_chunks: 20,
      total_size_bytes: 5000,
      documents_by_status: { processed: 5 },
    });
    vi.mocked(listDocuments).mockImplementation(async (options?: { folderId?: number }) =>
      options?.folderId === 9
        ? { documents: [], total: 0 }
        : {
            documents: [
              {
                id: '1',
                filename: 'outside-folder.pdf',
                size: 1024,
                created_at: '2024-01-01',
                metadata: { status: 'processed', chunk_count: 1 },
              },
            ],
            total: 1,
          }
    );

    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByText('Empty Folder'));
    });
    await waitFor(() => {
      expect(vi.mocked(listDocuments)).toHaveBeenCalledWith(
        expect.objectContaining({ folderId: 9 })
      );
    });
    await act(async () => {
      await flushMicrotasks();
    });

    // The vault HAS documents; an empty filter result is not "No documents
    // yet". RED at base, GREEN after the fix.
    expect(screen.queryAllByText('No documents yet').length).toBe(0);
  });

  it('single delete confirmation names the document', async () => {
    // CHECK_WRONG amend: earlier tests in this file override listDocuments
    // with mockImplementation (clearAllMocks does not reset implementations),
    // so re-seed the factory default fixture this check was authored against.
    vi.mocked(listDocuments).mockResolvedValue({
      documents: [
        { id: '1', filename: 'test.pdf', size: 1024, created_at: '2024-01-01', metadata: { status: 'processed', chunk_count: 5 } },
        { id: '2', filename: 'test2.pdf', size: 2048, created_at: '2024-01-02', metadata: { status: 'processed', chunk_count: 10 } },
      ],
      total: 2,
    });
    await renderPage();

    const deleteButtons = await screen.findAllByLabelText('Delete document');
    await act(async () => {
      fireEvent.click(deleteButtons[0]);
    });

    const { dialog } = await findDialogDestructiveButton();
    // The confirmation for test.pdf must say which document dies.
    // RED at base (static description), GREEN after the fix.
    expect((dialog.textContent ?? '').includes('test.pdf')).toBe(true);
  });

  it('single delete confirmation button is labelled Delete', async () => {
    await renderPage();

    const deleteButtons = await screen.findAllByLabelText('Delete document');
    await act(async () => {
      fireEvent.click(deleteButtons[0]);
    });

    const { dialog } = await findDialogDestructiveButton();
    // The destructive action must be labelled by what it does.
    // RED at base (ConfirmDialog hardcodes Confirm), GREEN after the fix.
    expect(within(dialog).queryAllByRole('button', { name: 'Delete' }).length).toBe(1);
  });
});
