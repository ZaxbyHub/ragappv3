import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render as rtlRender, fireEvent, act, waitFor, screen } from '@testing-library/react';
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

// Mock useAuthStore for the global-role gate (issue #767 AC5). The factory
// honours zustand-style selectors so the component's read works either way.
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
import { listDocuments } from '@/lib/api';

async function flushMicrotasks(rounds = 12) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

describe('DocumentsPage K04 permission truthfulness (issue #767)', () => {
  let container: HTMLElement;
  let unmount: (() => void) | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    authState.user = null;
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: null,
      vaults: [],
    } as ReturnType<typeof useVaultStore>);
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

  it('vault writer is offered folder creation', async () => {
    // Folder creation needs only vault WRITE on the server (_require_vault_write,
    // folders.py). RED at base (the gate demands admin), GREEN after the fix.
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: 2,
      vaults: [{ id: 2, name: 'Writer Vault', current_user_permission: 'write' }],
    } as ReturnType<typeof useVaultStore>);

    await renderPage();

    expect(screen.queryAllByLabelText('New folder').length).toBe(1);
  });

  it('vault writer can select rows for bulk tag and move', async () => {
    // Bulk tag assignment / move need only vault WRITE on the server
    // (tags.py, folders.py). RED at base (selection is admin-gated),
    // GREEN after the fix.
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: 2,
      vaults: [{ id: 2, name: 'Writer Vault', current_user_permission: 'write' }],
    } as ReturnType<typeof useVaultStore>);

    await renderPage();

    const selectAll = container.querySelector(
      'input[aria-label="Select all documents"]'
    ) as HTMLInputElement;
    expect(selectAll).toBeTruthy();
    expect(selectAll.disabled).toBe(false);
  });

  it('read only viewer gets no enabled wiki Compile control', async () => {
    // Wiki compile needs vault WRITE on the server (_require_vault_write,
    // wiki.py). RED at base (Compile renders enabled for any viewer),
    // GREEN after the fix.
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: 3,
      vaults: [{ id: 3, name: 'Read Vault', current_user_permission: 'read' }],
    } as ReturnType<typeof useVaultStore>);
    vi.mocked(listDocuments).mockResolvedValue({
      documents: [
        {
          id: '1',
          filename: 'readable.pdf',
          size: 1024,
          created_at: '2024-01-01',
          metadata: { status: 'indexed', chunk_count: 4 },
        },
      ],
      total: 1,
    });

    await renderPage();

    const enabledWikiControls = screen
      .getAllByRole('button')
      .filter((button) => {
        const name = button.getAttribute('aria-label') ?? button.textContent ?? '';
        return /^(Compile|Recompile)/.test(name.trim()) && !(button as HTMLButtonElement).disabled;
      });
    expect(enabledWikiControls.length).toBe(0);
  });

  it('Scan Directory is not enabled for a user without the global admin role', async () => {
    // POST /api/documents/scan depends on require_admin_role (global admin),
    // so a member must not get an enabled Scan button even as vault admin.
    // RED at base (the button gates only on vault write), GREEN after the fix.
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: 2,
      vaults: [{ id: 2, name: 'Admin Vault', current_user_permission: 'admin' }],
    } as ReturnType<typeof useVaultStore>);
    authState.user = {
      id: 7,
      username: 'member1',
      full_name: 'Member One',
      role: 'member',
      is_active: true,
    };

    await renderPage();

    const scanButtons = screen.queryAllByRole('button', { name: 'Scan Directory' });
    expect(scanButtons.length).toBeGreaterThan(0);
    const scanEnabled = scanButtons.some(
      (button) => !(button as HTMLButtonElement).disabled
    );
    expect(scanEnabled).toBe(false);
  });
});
