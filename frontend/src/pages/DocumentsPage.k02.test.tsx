// frontend/src/pages/DocumentsPage.k02.test.tsx
// k02 acceptance checks — cross-vault races on the DocumentsPage tag refetch
// and on a bulk-delete confirm dialog held open across a vault switch.
//
// Two arm's-length checks, both expected RED at base:
//   AC13 — a SLOW vault-1 listTags response that settles AFTER the user has
//          switched to vault 2 must never overwrite the vault-2 tags.
//          At base fetchTags (DocumentsPage.tsx) commits
//          `setTags(await listTags(activeVaultId))` unconditionally — the
//          still-pending vault-1 flight has no staleness guard, so the late
//          vault-1 payload replaces the vault-2 tag list.
//   AC14 — a bulk-delete confirm dialog OPENED while on vault 1 must not send
//          vault-1 document ids once the user has switched to vault 2 and
//          only then clicks Confirm. At base handleBulkDelete stores
//          `onConfirm: executeBulkDelete`, a closure over the pre-switch
//          selectedIds / canMutateDocuments; the dialog survives an
//          admin→admin switch, so Confirm fires deleteDocuments(["v1-a"]).
//
// Mock structure mirrors DocumentsPage.k01.test.tsx (proven in this repo):
// the mutable useVaultStore mockReturnValue + utils.rerender vault switch,
// the render-through @/components/ui/select stub (Radix Select options never
// mount in jsdom otherwise), the native-input checkbox mock, and the real
// ConfirmDialog (Radix portal, unmocked dialog primitives).

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render as rtlRender, fireEvent, act, waitFor, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import type { RenderResult } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

// DocumentsPage renders <Link> for document names; provide a router context.
// rerender() re-runs the wrapper, which is how the vault switch is driven:
// the useVaultStore mock returns a new value and the SAME component instance
// re-renders (selection/tags/confirm-dialog state preserved) — the exact
// scenario under test.
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
// plain text — required for the AC13 tag-visibility assertions.
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
import type { Document, Tag, ListDocumentsOptions } from '@/lib/api';

// ---------------------------------------------------------------------------
// Fixtures & helpers
// ---------------------------------------------------------------------------

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

const makeDoc = (id: string, filename: string, status = 'processed'): Document => ({
  id,
  filename,
  size: 1024,
  created_at: '2024-01-01',
  metadata: { status, chunk_count: 1 },
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('DocumentsPage k02 — cross-vault tag refetch race and pre-switch bulk delete', () => {
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

  it('late previous vault tag refetch never commits after a vault switch', { timeout: 30_000 }, async () => {
    // listTags: vault 1's call is HELD (deferred promise); vault 2's call
    // resolves immediately. The vault-1 list is one simple indexed row each
    // way so no processing/poll noise interferes.
    let resolveV1Tags!: (tags: Tag[]) => void;
    const heldV1Tags = new Promise<Tag[]>((resolve) => {
      resolveV1Tags = resolve;
    });
    vi.mocked(listTags).mockImplementation((vaultId: number) =>
      vaultId === 2 ? Promise.resolve([makeTag(20, 'v2-tag')]) : heldV1Tags
    );
    vi.mocked(listDocuments).mockImplementation(async (options: ListDocumentsOptions = {}) =>
      options.vaultId === 2
        ? { documents: [makeDoc('v2-a', 'v2-a.pdf', 'indexed')], total: 1 }
        : { documents: [makeDoc('v1-a', 'v1-a.pdf', 'indexed')], total: 1 }
    );

    let utils!: RenderResult;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    // Guard: the vault-1 tag fetch is IN FLIGHT (called, unresolved).
    await waitFor(() => expect(vi.mocked(listTags)).toHaveBeenCalledWith(1), { timeout: 10_000 });

    // Switch to vault 2 (same component instance, admin→admin) — the vault-2
    // tag refetch fires and succeeds.
    setStoreVault(2);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    await waitFor(
      () =>
        expect(screen.queryAllByText((content: string) => content.includes('v2-tag')).length).toBeGreaterThan(
          0
        ),
      { timeout: 10_000 }
    );

    // NOW the held vault-1 response settles — after the switch. It must never
    // overwrite the vault-2 tags.
    await act(async () => {
      resolveV1Tags([makeTag(10, 'v1-only-tag')]);
      await flushMicrotasks();
    });

    // Discriminating assertion: no element may show the late vault-1 tag. At
    // base fetchTags commits the late payload unconditionally, so the
    // SelectItem "v1-only-tag (0)" IS rendered → count 1 → RED.
    console.log(
      'K02/AC13 CHECK: FAIL — expecting zero elements showing "v1-only-tag" after the late vault-1 listTags response settles post-switch'
    );
    const lateTagCount = screen.queryAllByText((content: string) => content.includes('v1-only-tag')).length;
    expect(lateTagCount).toBe(0);
  });

  it('bulk delete dialog opened before a vault switch sends no previous vault ids', { timeout: 30_000 }, async () => {
    // Same render shape as k01's AC1 check: admin vault 1 with row v1-a.pdf,
    // vault 2 with only v2-a.pdf.
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

    // OPEN the confirm dialog — do NOT click Confirm yet.
    const deleteSelected = screen.getByRole('button', { name: 'Delete Selected' });
    await act(async () => {
      fireEvent.click(deleteSelected);
    });
    // Guard: the dialog is actually open (portal rendered by this same
    // component instance, so it survives the vault switch below).
    expect(screen.getByText('Delete Selected Documents')).toBeInTheDocument();

    // Switch to vault 2 (same component instance, admin→admin) — the dialog
    // stays open with the pre-switch onConfirm closure.
    setStoreVault(2);
    await act(async () => {
      utils.rerender(<DocumentsPage />);
    });
    // Guard: the rerender landed (vault-2 row rendered, dialog still open).
    await waitFor(
      () => expect(screen.queryAllByText('v2-a.pdf').length).toBeGreaterThan(0),
      { timeout: 10_000 }
    );
    expect(screen.getByText('Delete Selected Documents')).toBeInTheDocument();

    // Click the dialog's Confirm. Post-fix the dialog may legitimately be gone
    // (cleared on switch) — then there is nothing to click and trivially no
    // vault-1 id can be sent; at base it is open and clicking sends the
    // pre-switch selection.
    const confirmAfterSwitch = screen.queryByRole('button', { name: 'Confirm' });
    if (confirmAfterSwitch) {
      await act(async () => {
        fireEvent.click(confirmAfterSwitch);
        await flushMicrotasks();
      });
    }

    // Discriminating assertion: across ALL deleteDocuments calls, the count of
    // vault-1 document ids ("v1-a") must be 0. At base the captured
    // executeBulkDelete closure sends ["v1-a"] → count 1 → RED. The check
    // passes whether the fix clears the dialog, aborts at execute time, or
    // clears the selection — it only asserts no vault-1 id reaches
    // deleteDocuments.
    console.log(
      'K02/AC14 CHECK: FAIL — expecting zero vault-1 ids ("v1-a") across all deleteDocuments calls after confirming a pre-switch dialog'
    );
    const staleIdCount = vi.mocked(deleteDocuments)
      .mock.calls.flatMap((call) => call[0] ?? [])
      .filter((id) => String(id) === 'v1-a').length;
    expect(staleIdCount).toBe(0);
  });
});
