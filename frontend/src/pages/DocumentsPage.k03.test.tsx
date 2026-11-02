// frontend/src/pages/DocumentsPage.k03.test.tsx
// k03 feedback-round pins (PR #1012 swarm-pr-feedback): guard positive-direction,
// delete-all guard+refetch, folders late-success, in-flight vocabulary window.
//
// Six pins over the uncommitted PR #1012 working-tree fixes, all expected GREEN
// against the current working tree:
//   1. executeDeleteAllInVault's execute-time vault guard must REFUSE a
//      delete-all Confirm clicked after a vault switch (the API is never
//      called; the "Vault changed" toast fires).
//   2/3. Positive direction of the same guards: a SAME-vault confirm must not
//      be refused — bulk delete sends the selection, delete-all calls the API.
//   4. delete-all success must refetch (executeDeleteAllInVault ends with
//      `await fetchDocuments()`) so the polling hook's `total` cannot leave a
//      stale "Showing 0 of N" line + Load-more control behind.
//   5. A LATE vault-1 listFolders success settling after a vault switch must
//      never commit (fetchFolders' issued-vault guard).
//   6. The RESET-during-in-flight window (not the late-success race, which the
//      frozen C13 covers): while the next vault's listTags is still PENDING,
//      the previous vault's tag vocabulary must already be gone from the UI.
//
// Mock structure mirrors DocumentsPage.k02.test.tsx (proven in this repo): the
// mutable useVaultStore mockReturnValue + utils.rerender vault switch, the
// render-through @/components/ui/select stub (Radix Select options never mount
// in jsdom otherwise), the native-input checkbox mock, and the real
// ConfirmDialog (Radix portal, unmocked dialog primitives).

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render as rtlRender, fireEvent, act, waitFor, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import type { RenderResult } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

// DocumentsPage renders <Link> for document names; provide a router context.
// rerender() re-runs the wrapper, which is how the vault switch is driven: the
// useVaultStore mock returns a new value and the SAME component instance
// re-renders (confirm-dialog state preserved) — the exact scenario under test.
const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

// #767 AC5: DocumentsPage now reads the user's global role (the scan route
// requires GLOBAL admin); mock the auth store so the real module (with its
// module-level attachCsrfInterceptor side effects and wide import graph)
// never loads in this suite.
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

// Radix Select cannot be driven in jsdom (no pointer-capture support), so we
// mock the primitive with a context that renders every SelectItem's label as
// plain text — required for the tag-visibility assertions.
vi.mock('@/components/ui/select', async () => {
  const React = await import('react');
  const Ctx = React.createContext<(v: string) => void>(() => {});
  return {
    Select: ({ onValueChange, children }: { onValueChange: (v: string) => void; children: React.ReactNode }) =>
      React.createElement(Ctx.Provider, { value: onValueChange }, children),
    SelectTrigger: ({ children, 'aria-label': ariaLabel }: { children: React.ReactNode; 'aria-label'?: string }) =>
      React.createElement('div', { role: 'group', 'aria-label': ariaLabel }, children),
    SelectValue: ({ placeholder }: { placeholder?: string }) => React.createElement('span', null, placeholder),
    SelectContent: ({ children }: { children: React.ReactNode }) => React.createElement('div', null, children),
    SelectItem: ({ value, children }: { value: string; children: React.ReactNode }) => {
      const onValueChange = React.useContext(Ctx);
      return React.createElement('button', { onClick: () => onValueChange(value) }, children);
    },
  };
});

// Mock API. Defaults are re-seeded in beforeEach (vi.clearAllMocks keeps
// implementations, but re-seeding makes each test self-describing).
vi.mock('@/lib/api', () => ({
  listDocuments: vi.fn().mockResolvedValue({ documents: [], total: 0 }),
  scanDocuments: vi.fn().mockResolvedValue({ added: 0, scanned: 0 }),
  deleteDocument: vi.fn().mockResolvedValue(undefined),
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
    total_documents: 0,
    total_chunks: 0,
    total_size_bytes: 0,
    documents_by_status: {},
  }),
  listTags: vi.fn().mockResolvedValue([]),
  listFolders: vi.fn().mockResolvedValue([]),
  createFolder: vi.fn().mockResolvedValue({ id: 1, name: 'mock', vault_id: 1, parent_folder_id: null }),
  updateFolder: vi.fn().mockResolvedValue({ id: 1, name: 'updated', vault_id: 1, parent_folder_id: null }),
  deleteFolder: vi.fn().mockResolvedValue(undefined),
  downloadDocument: vi.fn().mockResolvedValue(undefined),
  // Imported (module-scope) by dialogs rendered with open=false on this page.
  assignTags: vi.fn().mockResolvedValue([]),
  createTag: vi.fn().mockResolvedValue({}),
  moveDocumentsToFolder: vi.fn().mockResolvedValue({}),
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

// Mock useDebounce hook — pass-through: [value, false].
vi.mock('@/hooks/useDebounce', () => ({
  useDebounce: vi.fn((value: string) => [value, false]),
}));

// Mock useVaultStore — the vault switch flips this mock's return value and
// re-renders the page through utils.rerender().
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

// EmptyState mock mirroring the real component's action discrimination
// (`action?: ReactNode | EmptyStateAction`).
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
import { toast } from 'sonner';
import {
  listDocuments,
  listTags,
  listFolders,
  deleteDocuments,
  deleteAllDocumentsInVault,
  getDocumentStats,
  getDocumentWikiStatus,
} from '@/lib/api';
import type { Document, Tag, Folder, ListDocumentsOptions } from '@/lib/api';

// ---------------------------------------------------------------------------
// Fixtures & helpers
// ---------------------------------------------------------------------------

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

const makeDoc = (id: string, filename: string): Document => ({
  id,
  filename,
  size: 1024,
  created_at: '2024-01-01',
  metadata: { status: 'processed', chunk_count: 1 },
});

const makeTag = (id: number, name: string): Tag => ({
  id,
  vault_id: 1,
  name,
  color: '',
  created_at: '2024-01-01',
  updated_at: '2024-01-01',
  document_count: 0,
});

const makeFolder = (id: number, name: string): Folder => ({
  id,
  vault_id: 1,
  parent_folder_id: null,
  name,
  description: '',
  created_at: '2024-01-01',
  updated_at: '2024-01-01',
  document_count: 0,
});

// Both vaults administered by the user — the selection-clearing effect only
// fires when canMutateDocuments flips false, so an admin→admin switch never
// clears it, and the confirm dialog (component state) survives the switch.
const VAULTS = [
  { id: 1, name: 'Vault One', current_user_permission: 'admin' },
  { id: 2, name: 'Vault Two', current_user_permission: 'admin' },
];

function setStoreVault(vaultId: number | null) {
  vi.mocked(useVaultStore).mockReturnValue({
    activeVaultId: vaultId,
    vaults: VAULTS,
  } as ReturnType<typeof useVaultStore>);
}

const STATS = {
  total_documents: 0,
  total_chunks: 0,
  total_size_bytes: 0,
  documents_by_status: {},
};

/** Vault-keyed document list: vault 1 shows v1-a.pdf, vault 2 shows v2-a.pdf. */
function listByVault() {
  return vi.mocked(listDocuments).mockImplementation(async (options: ListDocumentsOptions = {}) =>
    options.vaultId === 2
      ? { documents: [makeDoc('v2-a', 'v2-a.pdf')], total: 1 }
      : { documents: [makeDoc('v1-a', 'v1-a.pdf')], total: 1 }
  );
}

/** Guard: wait until the vault-switch rerender's rows are on screen. */
async function waitForVaultRows(filename: string) {
  await waitFor(
    () => expect(screen.queryAllByText(filename).length).toBeGreaterThan(0),
    { timeout: 10_000 }
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('DocumentsPage k03 — feedback-round pins (delete-all guard/refetch, folder + tag races)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setStoreVault(1);
    vi.mocked(listDocuments).mockResolvedValue({ documents: [], total: 0 });
    vi.mocked(listTags).mockResolvedValue([]);
    vi.mocked(listFolders).mockResolvedValue([]);
    vi.mocked(getDocumentStats).mockResolvedValue(STATS);
    vi.mocked(getDocumentWikiStatus).mockResolvedValue({
      wiki_status: 'not_compiled',
      pages_count: 0,
      claims_count: 0,
      lint_count: 0,
    });
    vi.mocked(deleteDocuments).mockResolvedValue({ deleted_count: 0, failed_ids: [] });
    vi.mocked(deleteAllDocumentsInVault).mockResolvedValue({ deleted_count: 0 });
  });

  it('delete-all dialog opened before a vault switch refuses to call the API', { timeout: 30_000 }, async () => {
    listByVault();

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    // Open the delete-all confirm on vault 1 (needs an admin vault with rows).
    const deleteAllButton = await screen.findByRole('button', { name: 'Delete All in Vault' }, { timeout: 10_000 });
    await act(async () => {
      fireEvent.click(deleteAllButton);
    });
    // Guard: the dialog is actually open.
    expect(await screen.findByText('Delete All Documents in Vault')).toBeInTheDocument();

    // Switch to vault 2 (same component instance, admin→admin) — the dialog
    // stays open with the pre-switch executeDeleteAllInVault closure.
    setStoreVault(2);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    // Guard: the rerender landed (vault-2 rows rendered, dialog still open).
    await waitForVaultRows('v2-a.pdf');
    expect(screen.getByText('Delete All Documents in Vault')).toBeInTheDocument();

    // Confirm through the stale dialog — the execute-time vault guard must
    // refuse BEFORE deleteAllDocumentsInVault is reached.
    const confirm = screen.getByRole('button', { name: 'Confirm' });
    await act(async () => {
      fireEvent.click(confirm);
      await flushMicrotasks();
    });

    expect(vi.mocked(deleteAllDocumentsInVault)).not.toHaveBeenCalled();
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(expect.stringContaining('Vault changed'));
  });

  it('same-vault bulk delete confirm calls deleteDocuments with the selection', { timeout: 30_000 }, async () => {
    vi.mocked(listDocuments)
      .mockResolvedValueOnce({ documents: [makeDoc('v1-a', 'v1-a.pdf')], total: 1 })
      .mockResolvedValue({ documents: [], total: 0 });
    vi.mocked(deleteDocuments).mockResolvedValue({ deleted_count: 1, failed_ids: [] });

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    // Select the vault-1 row via its checkbox (aria-label "Select <filename>").
    const rowCheckbox = await screen.findByLabelText('Select v1-a.pdf', {}, { timeout: 10_000 });
    await act(async () => {
      fireEvent.click(rowCheckbox);
    });
    await waitFor(() => expect(screen.getByText('1 selected')).toBeInTheDocument(), { timeout: 5_000 });

    // Open the confirm and confirm — no vault switch, so the guard must NOT
    // refuse the legit confirm (positive direction of the AC14 fix).
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Delete Selected' }));
    });
    expect(screen.getByText('Delete Selected Documents')).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
      await flushMicrotasks();
    });

    expect(vi.mocked(deleteDocuments)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(deleteDocuments)).toHaveBeenCalledWith(['v1-a']);
  });

  it('same-vault delete-all confirm calls deleteAllDocumentsInVault', { timeout: 30_000 }, async () => {
    vi.mocked(listDocuments).mockResolvedValue({ documents: [makeDoc('v1-a', 'v1-a.pdf')], total: 1 });

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    const deleteAllButton = await screen.findByRole('button', { name: 'Delete All in Vault' }, { timeout: 10_000 });
    await act(async () => {
      fireEvent.click(deleteAllButton);
    });
    expect(await screen.findByText('Delete All Documents in Vault')).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
      await flushMicrotasks();
    });

    expect(vi.mocked(deleteAllDocumentsInVault)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(deleteAllDocumentsInVault)).toHaveBeenCalledWith(1);
  });

  it('delete-all success refetches so no stale total or load-more remains', { timeout: 30_000 }, async () => {
    // Initial: one row, total 1. The post-delete refetch (2nd call): empty, 0.
    let listCalls = 0;
    vi.mocked(listDocuments).mockImplementation(async (options: ListDocumentsOptions = {}) => {
      listCalls += 1;
      return listCalls === 1
        ? { documents: [makeDoc('v1-a', 'v1-a.pdf')], total: 1 }
        : { documents: [], total: 0 };
    });
    vi.mocked(deleteAllDocumentsInVault).mockResolvedValue({ deleted_count: 1, vault_id: 1 });

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    const deleteAllButton = await screen.findByRole('button', { name: 'Delete All in Vault' }, { timeout: 10_000 });
    await act(async () => {
      fireEvent.click(deleteAllButton);
    });
    expect(await screen.findByText('Delete All Documents in Vault')).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    });

    // The fix ends executeDeleteAllInVault with `await fetchDocuments()` —
    // exactly two listDocuments calls (initial + authoritative refetch).
    await waitFor(() => expect(vi.mocked(listDocuments)).toHaveBeenCalledTimes(2), { timeout: 10_000 });
    await act(async () => {
      await flushMicrotasks();
    });

    // Without the refetch, the hook's total stays 1 over 0 visible rows and
    // the "Showing 0 of 1 documents" block + Load-more button render.
    expect(screen.queryByRole('button', { name: /load more/i })).toBeNull();
    expect(screen.queryByText((content: string) => content.includes('Showing 0 of 1'))).toBeNull();
    // The refetch was issued for the CURRENT (still vault 1) vault.
    expect(vi.mocked(listDocuments)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(listDocuments).mock.calls[1][0]).toMatchObject({ vaultId: 1 });
  });

  it('folders late success after a vault switch never commits', { timeout: 30_000 }, async () => {
    // Vault 1's listFolders is HELD; vault 2's resolves [] immediately.
    let resolveV1Folders!: (folders: Folder[]) => void;
    const heldV1Folders = new Promise<Folder[]>((resolve) => {
      resolveV1Folders = resolve;
    });
    vi.mocked(listFolders).mockImplementation((vaultId: number) =>
      vaultId === 2 ? Promise.resolve([]) : heldV1Folders
    );
    // FolderTree renders beside the list only when a vault is selected; the
    // document list itself is irrelevant here — keep it simple both ways.
    vi.mocked(listDocuments).mockImplementation(async (options: ListDocumentsOptions = {}) =>
      options.vaultId === 2 ? { documents: [], total: 0 } : { documents: [], total: 0 }
    );

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });
    // Guard: the vault-1 folder fetch is IN FLIGHT (called, unresolved).
    await waitFor(() => expect(vi.mocked(listFolders)).toHaveBeenCalledWith(1), { timeout: 10_000 });

    // Switch to vault 2 — its folder fetch resolves [] and commits.
    setStoreVault(2);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    await waitFor(() => expect(vi.mocked(listFolders)).toHaveBeenCalledWith(2), { timeout: 10_000 });
    await act(async () => {
      await flushMicrotasks();
    });

    // NOW the held vault-1 response settles — after the switch. fetchFolders'
    // issued-vault guard must drop it (FolderTree renders folder names, so a
    // commit would put "V1 Only Folder" on screen — see k01's AC4 positive).
    await act(async () => {
      resolveV1Folders([makeFolder(1, 'V1 Only Folder')]);
      await flushMicrotasks();
    });

    expect(screen.queryAllByText('V1 Only Folder').length).toBe(0);
  });

  it('in-flight tag refetch after a vault switch shows no stale vocabulary', { timeout: 30_000 }, async () => {
    // Vault 1's listTags resolves and renders; vault 2's is HELD pending.
    let resolveV2Tags!: (tags: Tag[]) => void;
    const heldV2Tags = new Promise<Tag[]>((resolve) => {
      resolveV2Tags = resolve;
    });
    vi.mocked(listTags).mockImplementation((vaultId: number) =>
      vaultId === 2 ? heldV2Tags : Promise.resolve([makeTag(10, 'v1-only-tag')])
    );

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    // Guard: the vault-1 tag vocabulary is actually rendered (TagFilter's
    // SelectItem shows "v1-only-tag (0)").
    await waitFor(
      () =>
        expect(
          screen.queryAllByText((content: string) => content.includes('v1-only-tag')).length
        ).toBeGreaterThan(0),
      { timeout: 10_000 }
    );

    // Switch to vault 2 — its listTags stays PENDING. This pins the RESET
    // during the in-flight window: the switch effect clears tags immediately,
    // so the vault-1 vocabulary must already be gone while the fetch pends.
    setStoreVault(2);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    await waitFor(() => expect(vi.mocked(listTags)).toHaveBeenCalledWith(2), { timeout: 10_000 });

    expect(screen.queryAllByText((content: string) => content.includes('v1-only-tag')).length).toBe(0);

    // Settle the held vault-2 fetch so nothing outlives the test.
    await act(async () => {
      resolveV2Tags([]);
      await flushMicrotasks();
    });
    expect(screen.queryAllByText((content: string) => content.includes('v1-only-tag')).length).toBe(0);
  });
  // --- ABA (vault 1 -> 2 -> 1) round-trip pins (fifth-review finding) -----------

  it('ABA vault round trip leaves a stale bulk-delete confirm refused', { timeout: 30_000 }, async () => {
    // Vault-aware rows so every ABA leg renders its own vault's row.
    vi.mocked(listDocuments).mockImplementation((request?: { vaultId?: number }) =>
      Promise.resolve({
        documents: [makeDoc(`v${request?.vaultId ?? 1}-a`, `v${request?.vaultId ?? 1}-a.pdf`)],
        total: 1,
      })
    );
    vi.mocked(deleteDocuments).mockResolvedValue({ deleted_count: 1, failed_ids: [] });

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    // Select a vault-1 row and open the confirm dialog there.
    const rowCheckbox = await screen.findByLabelText('Select v1-a.pdf', {}, { timeout: 10_000 });
    await act(async () => {
      fireEvent.click(rowCheckbox);
    });
    await waitFor(() => expect(screen.getByText('1 selected')).toBeInTheDocument(), { timeout: 5_000 });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Delete Selected' }));
    });
    expect(screen.getByText('Delete Selected Documents')).toBeInTheDocument();

    // ABA: switch away and BACK to the same numeric vault while the dialog
    // stays open. The numeric-id compare (confirmVaultRef vs activeVaultIdRef)
    // sees 1 === 1 after the round trip and would pass; only the monotone
    // confirmation generation (bumped by each switch effect) still refuses.
    setStoreVault(2);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    await waitForVaultRows('v2-a.pdf');
    setStoreVault(1);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    await waitForVaultRows('v1-a.pdf');
    expect(screen.getByText('Delete Selected Documents')).toBeInTheDocument();

    // Confirm through the ABA-stale dialog: the guard must refuse.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
      await flushMicrotasks();
    });

    expect(vi.mocked(deleteDocuments)).not.toHaveBeenCalled();
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(expect.stringContaining('Selection changed'));
  });

  it('ABA vault round trip leaves a stale delete-all confirm refused', { timeout: 30_000 }, async () => {
    listByVault();

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    const deleteAllButton = await screen.findByRole('button', { name: 'Delete All in Vault' }, { timeout: 10_000 });
    await act(async () => {
      fireEvent.click(deleteAllButton);
    });
    expect(await screen.findByText('Delete All Documents in Vault')).toBeInTheDocument();

    // ABA round trip with the dialog open.
    setStoreVault(2);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    await waitForVaultRows('v2-a.pdf');
    setStoreVault(1);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    await waitForVaultRows('v1-a.pdf');
    expect(screen.getByText('Delete All Documents in Vault')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
      await flushMicrotasks();
    });

    expect(vi.mocked(deleteAllDocumentsInVault)).not.toHaveBeenCalled();
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(expect.stringContaining('Vault changed'));
  });
});
