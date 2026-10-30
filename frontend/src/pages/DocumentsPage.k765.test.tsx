// frontend/src/pages/DocumentsPage.k765.test.tsx
// K765 acceptance check (issue #765, frozen): C6 — a FAILED wiki status
// fetch must not be rendered as the "Compile" affordance.
//
// The documents table treats a missing wikiStatusMap entry ("!ws") the same
// as "not_compiled" and renders the Compile button. When the wiki status
// fetch itself FAILED, the status is UNKNOWN — not "never compiled" — so the
// page must not invite a compile from that state.
//
// Mock structure mirrors DocumentsPage.k03.test.tsx (proven in this repo):
// the full "@/lib/api" mock, the sonner callable toast mock, the
// useVaultStore/useUploadStore/useDebounce mocks, the UI-primitive stubs,
// the react-virtual stub, and the real ConfirmDialog. RED at base, GREEN
// after the fix.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render as rtlRender, act, waitFor, screen, fireEvent } from '@testing-library/react';
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
  // useUploadMonitoring imports these two-hops via this barrel; explicit
  // mocks keep them dead in this suite (latent today: the hook stays
  // disarmed with no uploads).
  getDocumentStatus: vi.fn(),
  getDocumentStatuses: vi.fn(),
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
  assignTags: vi.fn().mockResolvedValue([]),
  createTag: vi.fn().mockResolvedValue({}),
  moveDocumentsToFolder: vi.fn().mockResolvedValue({}),
}));

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

vi.mock('@/hooks/useDebounce', () => ({
  useDebounce: vi.fn((value: string) => [value, false]),
}));

vi.mock('@/stores/useVaultStore', () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: null,
    vaults: [],
  })),
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
  }: {
    title: string;
    description?: string;
  }) => (
    <div data-testid="empty-state">
      <h2>{title}</h2>
      {description && <p>{description}</p>}
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

vi.mock('@/components/documents/RejectedFilesBanner', () => ({
  RejectedFilesBanner: () => null,
}));

import DocumentsPage from '@/pages/DocumentsPage';
import { useVaultStore } from '@/stores/useVaultStore';
import { getDocumentWikiStatus, getDocumentStats, listDocuments } from '@/lib/api';
import type { Document } from '@/lib/api';

const INDEXED_DOC: Document = {
  id: '1',
  filename: 'probe.pdf',
  size: 1024,
  created_at: '2024-01-01',
  metadata: { status: 'indexed', chunk_count: 3 },
} as unknown as Document;

const STATS = {
  total_documents: 1,
  total_chunks: 3,
  total_size_bytes: 1024,
  documents_by_status: { indexed: 1 },
};

describe('DocumentsPage K765 acceptance check — failed wiki status fetch is not rendered as Compile', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: 1,
      vaults: [{ id: 1, name: 'K765 Vault', current_user_permission: 'admin' }],
    } as ReturnType<typeof useVaultStore>);
    vi.mocked(listDocuments).mockResolvedValue({ documents: [INDEXED_DOC], total: 1 });
    vi.mocked(getDocumentStats).mockResolvedValue(STATS);
    // The check's premise: EVERY wiki status fetch fails.
    vi.mocked(getDocumentWikiStatus).mockRejectedValue(new Error('wiki status unavailable'));
  });

  afterEach(() => {
    if (vi.isFakeTimers()) vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('failed wiki status fetch is not rendered as Compile', { timeout: 30_000 }, async () => {
    await act(async () => {
      render(<DocumentsPage />);
    });

    // Sentinel: the indexed row actually rendered and its wiki status fetch
    // was attempted (and failed).
    await waitFor(
      () => expect(vi.mocked(getDocumentWikiStatus).mock.calls.length).toBeGreaterThan(0),
      { timeout: 10_000 },
    );
    await act(async () => {
      for (let i = 0; i < 10; i += 1) await Promise.resolve();
    });

    // Positive control (review round 1): the failed fetch state actually
    // RENDERED — DocumentTable shows the "Wiki status unavailable" cell for
    // failed ids and the row (filename) is still visible. Without this the
    // zero-Compile assertion below could pass on a page that never rendered
    // the row's wiki cell at all.
    expect(await screen.findByText('Wiki status unavailable', {}, { timeout: 10_000 })).toBeInTheDocument();
    // The row's filename is still visible (table link and/or mobile card).
    expect(screen.getAllByText('probe.pdf').length).toBeGreaterThan(0);

    // Discriminating assertion: an UNKNOWN wiki status (the fetch failed)
    // must not render the Compile affordance — that invites a compile we
    // cannot know is needed. At base the missing map entry is treated as
    // "not_compiled" and one Compile button renders.
    // RED at base, GREEN after the fix.
    expect(screen.queryAllByRole('button', { name: 'Compile' }).length).toBe(0);
  });

  // Review round 2, PRR-037/B-2/R03 — the two-step shape the final critic
  // asked for: a CACHED status plus a LATER failed refresh. A cached
  // not_compiled renders Compile; when a subsequent refresh of that same
  // document FAILS, the current failure must take precedence over the stale
  // cached value and render "Wiki status unavailable" instead — the failure
  // must never be masked by the value it superseded.
  it('a cached status plus a later failed refresh renders unavailable, not Compile', { timeout: 30_000 }, async () => {
    // Step 1: the first hydration SUCCEEDS with not_compiled (the cell
    // renders the Compile affordance); every later fetch of this document
    // REJECTS.
    vi.mocked(getDocumentWikiStatus)
      .mockResolvedValueOnce({
        wiki_status: 'not_compiled',
        pages_count: 0,
        claims_count: 0,
        lint_count: 0,
      })
      .mockRejectedValue(new Error('wiki status unavailable'));

    await act(async () => {
      render(<DocumentsPage />);
    });

    // Sentinel: the cached not_compiled committed and the Compile button IS
    // on screen — the exact state a later failure must mask.
    const compileButton = await screen.findByRole(
      'button',
      { name: 'Compile' },
      { timeout: 10_000 },
    );
    expect(vi.mocked(getDocumentWikiStatus)).toHaveBeenCalledTimes(1);

    // Step 2: drive a later refresh of the SAME document through the compile
    // follow-up path (compileDocumentWiki resolves; the 2 s follow-up timer
    // re-fetches every indexed row). A search-string rerender would NOT work
    // here: the hydration pass skips documents with a cached non-transient
    // status, so the compile follow-up is the workable driver.
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'],
    });
    await act(async () => {
      fireEvent.click(compileButton);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await act(async () => {
      for (let i = 0; i < 10; i += 1) await Promise.resolve();
    });

    // The follow-up refresh ran and failed.
    expect(vi.mocked(getDocumentWikiStatus)).toHaveBeenCalledTimes(2);

    // Discriminating assertions: the failed refresh is rendered as
    // "Wiki status unavailable" and the cached not_compiled no longer
    // surfaces the Compile affordance.
    expect(screen.getByText('Wiki status unavailable')).toBeInTheDocument();
    expect(screen.queryAllByRole('button', { name: 'Compile' }).length).toBe(0);
  });

  it('a cached skipped status plus a later failed refresh renders unavailable, not Skipped', { timeout: 30_000 }, async () => {
    // Critic round 3: the failure branch must sit ABOVE the skipped
    // interpretation too — a cached skipped value plus a later failed
    // refresh must render the unavailable state, not the stale Skipped
    // label. Killing mutation: moving the failure branch back below the
    // skipped branch fails the final assertions. Driver: a Compile click on
    // the SIBLING document — the compile follow-up re-fetches EVERY indexed
    // row regardless of cache, which is the only cached-skipped-reachable
    // refetch (hydration skips cached non-transient docs; the retry interval
    // owns only failed ids).
    const DOC5: Document = {
      id: '5',
      filename: 'skipped.pdf',
      size: 1024,
      created_at: '2024-01-01',
      metadata: { status: 'indexed', chunk_count: 3 },
    } as unknown as Document;
    const DOC6: Document = {
      id: '6',
      filename: 'sibling.pdf',
      size: 2048,
      created_at: '2024-01-02',
      metadata: { status: 'indexed', chunk_count: 4 },
    } as unknown as Document;
    vi.mocked(listDocuments).mockResolvedValue({
      documents: [DOC5, DOC6],
      total: 2,
    });
    let doc5Fetches = 0;
    vi.mocked(getDocumentWikiStatus).mockImplementation((fileId: number) => {
      if (Number(fileId) === 5) {
        doc5Fetches += 1;
        if (doc5Fetches > 1) {
          return Promise.reject(new Error('status fetch failed'));
        }
        return Promise.resolve({
          file_id: 5,
          wiki_status: 'skipped',
          pages_count: 0,
          claims_count: 0,
          active_claims: 0,
          lint_count: 0,
          pages: [],
          latest_job: null,
          job_count: 0,
        });
      }
      return Promise.resolve({
        file_id: 6,
        wiki_status: 'not_compiled',
        pages_count: 0,
        claims_count: 0,
        active_claims: 0,
        lint_count: 0,
        pages: [],
        latest_job: null,
        job_count: 0,
      });
    });

    render(<DocumentsPage />);

    // Sentinel: doc 5's cached skipped committed and its label IS on screen.
    expect(
      await screen.findByText('Skipped — no extractable knowledge', {}, { timeout: 10_000 }),
    ).toBeInTheDocument();

    // Drive the compile follow-up from doc 6: the 2 s follow-up re-fetches
    // every indexed row — including cached-skipped doc 5 — and doc 5's
    // fetch fails this wave.
    const compileButton = await screen.findByRole(
      'button',
      { name: 'Compile' },
      { timeout: 10_000 },
    );
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'],
    });
    await act(async () => {
      fireEvent.click(compileButton);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await act(async () => {
      for (let i = 0; i < 10; i += 1) await Promise.resolve();
    });

    // Discriminating assertions: doc 5's later failure is rendered as
    // Wiki status unavailable and the stale Skipped label no longer masks
    // it. Under the killed ordering (failure branch below skipped) the
    // Skipped label would still be on screen.
    expect(screen.getByText('Wiki status unavailable')).toBeInTheDocument();
    expect(screen.queryByText('Skipped — no extractable knowledge')).not.toBeInTheDocument();
  });
});
