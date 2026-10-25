// frontend/src/pages/DocumentsPage.k01.test.tsx
// k01 acceptance checks — stale DocumentsPage state across vault switches
// and across a stats fetch failure.
//
// Four arm's-length checks, all expected RED at base:
//   AC1  — bulk delete after a vault switch must not send ids that came from
//          the previous vault (the stale selection survives because it is only
//          cleared when canMutateDocuments flips false; an admin→admin switch
//          never flips it).
//   AC4  — a FAILED folder refetch for the new vault must still hide the
//          previous vault's folders (fetchFolders' catch only logs; the
//          folders state is never reset).
//   AC5  — a FAILED tag refetch for the new vault must still hide the previous
//          vault's tags (fetchTags' catch only logs; tags state never reset).
//   AC10 — search results must render even when the stats fetch failed
//          (isResolvingSearchEmptyState = hasActiveSearch && !hasStats hides
//          the table behind a skeleton whenever stats are unavailable).
//
// Mock structure mirrors DocumentsPage.test.tsx / DocumentsPage.m01.test.tsx
// (proven in this repo), plus the render-through @/components/ui/select stub
// from src/tests/documents-organization.test.tsx (Radix Select options never
// mount in jsdom otherwise).

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render as rtlRender, fireEvent, act, waitFor, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import type { RenderResult } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

// DocumentsPage renders <Link> for document names; provide a router context.
// rerender() re-runs the wrapper, which is how the vault switch is driven:
// the useVaultStore mock returns a new value and the SAME component instance
// re-renders (selection/folders/tags state preserved) — the exact scenario.
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

// Radix Select cannot be driven in jsdom (no pointer-capture support), so we
// mock the primitive with a context that renders every SelectItem's label as
// plain text — required for the AC5 tag-visibility assertion.
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
import {
  listDocuments,
  listTags,
  listFolders,
  deleteDocuments,
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
// clears it (that is the base defect under AC1).
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('DocumentsPage k01 — stale state across vault switches and stats failure', () => {
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
  });

  it('bulk delete after a vault switch sends no id from the previous vault', { timeout: 30_000 }, async () => {
    // Vault 1 owns "v1-a"; vault 2 owns only "v2-a".
    vi.mocked(listDocuments).mockImplementation(async (options: ListDocumentsOptions = {}) =>
      options.vaultId === 2
        ? { documents: [makeDoc('v2-a', 'v2-a.pdf')], total: 1 }
        : { documents: [makeDoc('v1-a', 'v1-a.pdf')], total: 1 }
    );

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    // Select the vault-1 row via its checkbox (aria-label "Select <filename>").
    const rowCheckbox = await screen.findByLabelText('Select v1-a.pdf', {}, { timeout: 10_000 });
    await act(async () => {
      fireEvent.click(rowCheckbox);
    });
    // Guard: the click actually landed in the bulk-selection state.
    await waitFor(() => expect(screen.getByText('1 selected')).toBeInTheDocument(), { timeout: 5_000 });

    // Switch to vault 2 — same component instance, admin→admin, so at base
    // the selection is NOT cleared.
    setStoreVault(2);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });

    // The vault-2 list fully landed before we act on the toolbar.
    await waitFor(
      () => {
        expect(vi.mocked(listDocuments)).toHaveBeenCalledWith(expect.objectContaining({ vaultId: 2 }));
        expect(screen.queryAllByText('v2-a.pdf').length).toBeGreaterThan(0);
      },
      { timeout: 10_000 }
    );

    // Drive the bulk delete if the toolbar still offers it. Post-fix this
    // branch may legitimately not render (selection cleared or intersected);
    // either way no vault-1 id may reach deleteDocuments.
    const deleteSelected = screen.queryByRole('button', { name: 'Delete Selected' });
    if (deleteSelected) {
      await act(async () => {
        fireEvent.click(deleteSelected);
      });
      const confirm = await screen.findByRole('button', { name: 'Confirm' }, { timeout: 5_000 });
      await act(async () => {
        fireEvent.click(confirm);
        await flushMicrotasks();
      });
      await waitFor(() => expect(vi.mocked(deleteDocuments)).toHaveBeenCalled(), { timeout: 5_000 });
    }

    // Discriminating assertion: across ALL deleteDocuments calls, the count of
    // vault-1 document ids ("v1-a") must be 0. At base the stale selection is
    // sent → count 1 → RED.
    console.log(
      'K01/AC1 CHECK: FAIL — expecting zero vault-1 ids ("v1-a") across all deleteDocuments calls after a vault switch'
    );
    const staleIdCount = vi.mocked(deleteDocuments)
      .mock.calls.flatMap((call) => call[0] ?? [])
      .filter((id) => String(id) === 'v1-a').length;
    expect(staleIdCount).toBe(0);
  });

  it('failed folder refetch after a vault switch hides previous vault folders', { timeout: 30_000 }, async () => {
    vi.mocked(listFolders).mockImplementation(async (vaultId: number) => {
      if (vaultId === 1) return [makeFolder(1, 'V1 Only Folder')];
      throw new Error('vault 2 folders unavailable');
    });

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    // Vault 1's folder tree rendered.
    await screen.findByText('V1 Only Folder', {}, { timeout: 10_000 });

    // Switch to vault 2, whose folder list fetch FAILS.
    setStoreVault(2);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    await waitFor(() => expect(vi.mocked(listFolders)).toHaveBeenCalledWith(2), { timeout: 10_000 });
    // Let the rejection propagate through fetchFolders' await/catch.
    await act(async () => {
      await flushMicrotasks();
    });

    // At base the catch only logs and `folders` is never reset — the vault-1
    // folder name stays rendered → RED.
    console.log(
      'K01/AC4 CHECK: FAIL — expecting the vault-1 folder name to disappear after a failed folder refetch for vault 2'
    );
    expect(screen.queryAllByText('V1 Only Folder').length).toBe(0);
  });

  it('failed tag refetch after a vault switch hides previous vault tags', { timeout: 30_000 }, async () => {
    vi.mocked(listTags).mockImplementation(async (vaultId: number) => {
      if (vaultId === 1) return [makeTag(1, 'v1-only-tag')];
      throw new Error('vault 2 tags unavailable');
    });

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    // Vault 1's tag filter rendered (SelectItem label, via the render-through
    // select stub it is plain text: "v1-only-tag (0)").
    await waitFor(
      () =>
        expect(
          screen.queryAllByText((content: string) => content.includes('v1-only-tag')).length
        ).toBeGreaterThan(0),
      { timeout: 10_000 }
    );

    // Switch to vault 2, whose tag list fetch FAILS.
    setStoreVault(2);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    await waitFor(() => expect(vi.mocked(listTags)).toHaveBeenCalledWith(2), { timeout: 10_000 });
    await act(async () => {
      await flushMicrotasks();
    });

    // At base the catch only logs and `tags` is never reset — the vault-1 tag
    // option stays rendered → RED.
    console.log(
      'K01/AC5 CHECK: FAIL — expecting the vault-1 tag to disappear after a failed tag refetch for vault 2'
    );
    expect(screen.queryAllByText((content: string) => content.includes('v1-only-tag')).length).toBe(0);
  });

  it('search results render after a stats fetch failure', { timeout: 30_000 }, async () => {
    // Stats NEVER resolve; the list is empty without a search and returns one
    // matching row for search "match".
    vi.mocked(getDocumentStats).mockRejectedValue(new Error('stats unavailable'));
    vi.mocked(listDocuments).mockImplementation(async (options: ListDocumentsOptions = {}) =>
      options.search
        ? { documents: [makeDoc('9', 'match.pdf')], total: 1 }
        : { documents: [], total: 0 }
    );

    await act(async () => {
      render(<DocumentsPage />);
    });

    // Initial view settles on the plain empty state (no active search yet).
    await screen.findByText('No documents yet', {}, { timeout: 10_000 });

    const searchInput = screen.getByPlaceholderText('Search documents and metadata...') as HTMLInputElement;
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: 'match' } });
    });

    // The search query reached the API and its response committed.
    await waitFor(
      () => expect(vi.mocked(listDocuments)).toHaveBeenCalledWith(expect.objectContaining({ search: 'match' })),
      { timeout: 10_000 }
    );
    await act(async () => {
      await flushMicrotasks();
    });

    // At base isResolvingSearchEmptyState (active search + no stats) hides the
    // table behind a skeleton, so the matching row never renders → RED.
    console.log(
      'K01/AC10 CHECK: FAIL — expecting search results ("match.pdf") to render while the stats fetch has failed'
    );
    expect(screen.queryAllByText('match.pdf').length).toBeGreaterThan(0);
  });
});
