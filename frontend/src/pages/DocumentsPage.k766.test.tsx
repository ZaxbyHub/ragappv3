// frontend/src/pages/DocumentsPage.k766.test.tsx
// K766 acceptance checks (issue #766, frozen); all DISCRIMINATING (RED at
// base, GREEN after the fix). C12 — pagehide inside the 3s undo window
// dispatches the confirmed delete via the keepalive fetch when a CSRF
// credential is cached; with none it dispatches NOTHING and registers the
// id for the bfcache pageshow replay (which completes it via
// deleteDocument). C13 — a FAILED unmount flush surfaces an error toast
// (message pinned). C19 — tag/folder failure logs never serialize the
// request config / Authorization header (round 2: production NORMALIZED
// error fixture with positive pins for status 500 and the /documents URL).
// Round 2 PRR-007 — "ragapp:before-logout" (dispatched FIRST by logout,
// while credentials are valid) runs the SPA-unmount flush: the delete goes
// out via the normal path; a later pageshow cannot re-send it.
// Mocks mirror DocumentsPage.k03.test.tsx (proven in this repo): the full
// "@/lib/api" mock, the sonner toast mock, the store/debounce mocks, the
// UI-primitive stubs, the react-virtual stub, the real ConfirmDialog.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render as rtlRender, fireEvent, act, waitFor, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MemoryRouter } from 'react-router-dom';
import { AxiosError, type InternalAxiosRequestConfig } from 'axios';

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
  getDocumentWikiStatus: vi.fn().mockResolvedValue({ wiki_status: 'not_compiled', pages_count: 0, claims_count: 0, lint_count: 0 }),
  // useUploadMonitoring imports these two-hops via this barrel; explicit
  // mocks keep them dead in this suite.
  getDocumentStatus: vi.fn(),
  getDocumentStatuses: vi.fn(),
  compileDocumentWiki: vi.fn().mockResolvedValue({ job_id: 1, status: 'queued' }),
  getDocumentStats: vi.fn().mockResolvedValue({ total_documents: 0, total_chunks: 0, total_size_bytes: 0, documents_by_status: {} }),
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
// Import component after mocks.
import DocumentsPage from '@/pages/DocumentsPage';
import { useVaultStore } from '@/stores/useVaultStore';
import { toast } from 'sonner';
import { deleteDocument, getDocumentStats, listDocuments, listTags, listFolders } from '@/lib/api';
import type { Document } from '@/lib/api';

const DOC_ONE: Document = {
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

const VAULTS = [{ id: 1, name: 'K766 Vault', current_user_permission: 'admin' }];

function setAdminVault() {
  vi.mocked(useVaultStore).mockReturnValue({
    activeVaultId: 1,
    vaults: VAULTS,
  } as ReturnType<typeof useVaultStore>);
}

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

/** Confirm a single-row delete: row "Delete document" -> dialog destructive button. */
async function confirmSingleDelete() {
  const deleteButtons = await screen.findAllByLabelText('Delete document', {}, { timeout: 10_000 });
  await act(async () => {
    fireEvent.click(deleteButtons[0]);
  });
  // The real ConfirmDialog renders in a Radix portal; locate the destructive
  // confirm BY ROLE inside the dialog.
  const dialog = await screen.findByRole('dialog', {}, { timeout: 10_000 });
  const confirmButton = within(dialog).getByRole('button', { name: /confirm|delete/i });
  expect(confirmButton).toBeEnabled();
  await act(async () => {
    fireEvent.click(confirmButton);
  });
  // Guard: the confirm dialog closed (the confirm actually happened).
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 5_000 });
}

/** The apiClient interceptor's NORMALIZED error (mirrors the K766 C18
 * fixture in useDocumentPolling.k766.test.ts): top-level status, raw
 * AxiosError under originalError whose serialized config carries the
 * Authorization header and the request URL. */
function normalizedBearerError(): Error & { status?: number; originalError?: unknown } {
  const config = {
    headers: { Authorization: 'Bearer probe-token' },
    url: '/documents',
  } as unknown as InternalAxiosRequestConfig;
  const err = new Error('Request failed with status code 500') as Error & {
    status?: number;
    originalError?: unknown;
  };
  err.status = 500;
  err.originalError = new AxiosError('Request failed with status code 500', '500', config);
  return err;
}

/** Serialize every logged argument; JSON.stringify failures become "". */
function serializedLoggedArgs(spies: Array<{ mock: { calls: unknown[][] } }>): string[] {
  return spies
    .flatMap((spy) => spy.mock.calls)
    .flatMap((args) =>
      args.map((arg) => {
        try {
          const serialized = JSON.stringify(arg);
          return serialized === undefined ? '' : serialized;
        } catch {
          return '';
        }
      }),
    );
}

describe('DocumentsPage K766 acceptance checks', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  /** Count deleteDocument mock calls for doc "1". */
  function apiDeleteCalls(): number {
    return vi
      .mocked(deleteDocument)
      .mock.calls.filter(([docId]) => docId === '1').length;
  }

  /** Count keepalive fetch calls for doc "1" (shape asserted in the credential test). */
  function keepaliveCalls(): number {
    return fetchSpy.mock.calls.filter(([input, init]) => {
      const requestInit = (init ?? {}) as { keepalive?: boolean };
      return requestInit.keepalive === true && String(input).endsWith('/documents/1');
    }).length;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    setAdminVault();
    vi.mocked(listDocuments).mockResolvedValue({ documents: [DOC_ONE], total: 1 });
    vi.mocked(getDocumentStats).mockResolvedValue(STATS);
    vi.mocked(deleteDocument).mockResolvedValue(undefined);
    // The keepalive channel rides the REAL core's global fetch; stub it so
    // no real request leaves jsdom (sendBeacon is gone from the design).
    fetchSpy = vi.fn(() => Promise.resolve({ ok: true } as Response));
    vi.stubGlobal('fetch', fetchSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    // Any CSRF cookie seeded by a test must not leak into the next one.
    document.cookie = 'X-CSRF-Token=; path=/; max-age=0';
    vi.restoreAllMocks();
  });

  it('pagehide without a credential defers the delete and pageshow replays it', { timeout: 30_000 }, async () => {
    await act(async () => {
      render(<DocumentsPage />);
    });

    // Confirm one delete (starts the 3s undo window; no CSRF credential cached).
    await confirmSingleDelete();
    expect(vi.mocked(deleteDocument)).not.toHaveBeenCalled();

    // Leave the page WITHOUT unmounting: the tab is closing.
    await act(async () => {
      fireEvent(window, new Event('pagehide'));
    });

    // Discriminating assertion (first half): with NO credential the pagehide
    // dispatches NOTHING — the id is registered for the bfcache replay
    // instead (the replay below is what discriminates; at base pagehide
    // does nothing at all).
    expect(apiDeleteCalls()).toBe(0);
    expect(keepaliveCalls()).toBe(0);

    // The page returns from the bfcache (jsdom has no PageTransitionEvent —
    // synthesize the persisted flag).
    const pageshow = new Event('pageshow');
    (pageshow as unknown as { persisted: boolean }).persisted = true;
    await act(async () => {
      fireEvent(window, pageshow);
    });
    await act(async () => {
      await flushMicrotasks();
    });

    // Discriminating assertion (second half): the persisted pageshow REPLAYS
    // the registered delete through the normal path, exactly once.
    expect(apiDeleteCalls()).toBe(1);
  });

  it('pagehide with a cached CSRF credential issues the keepalive delete', { timeout: 30_000 }, async () => {
    // Seed the readable CSRF cookie the keepalive channel reads.
    document.cookie = 'X-CSRF-Token=test-token';

    let utils!: ReturnType<typeof render>;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });
    await confirmSingleDelete();
    expect(vi.mocked(deleteDocument)).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent(window, new Event('pagehide'));
    });

    // Discriminating assertion: the confirmed delete left via the keepalive
    // fetch — exactly one request with the destructive shape.
    expect(keepaliveCalls()).toBe(1);
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringMatching(/\/documents\/1$/),
      expect.objectContaining({
        method: 'DELETE',
        keepalive: true,
        headers: expect.objectContaining({ 'X-CSRF-Token': 'test-token' }),
      }),
    );
    expect(apiDeleteCalls()).toBe(0);

    // Bfcache return: the keepalive outcome is UNCONFIRMED, so the pageshow replay completes the intent.
    const pageshow = new Event('pageshow');
    (pageshow as unknown as { persisted: boolean }).persisted = true;
    await act(async () => {
      fireEvent(window, pageshow);
    });
    await act(async () => {
      await flushMicrotasks();
    });
    expect(apiDeleteCalls()).toBe(1);

    // Exactly-once pin: the pagehide flush emptied the pending map, so the
    // SPA-unmount flush finds nothing.
    await act(async () => {
      utils.unmount();
    });
    await act(async () => {
      await flushMicrotasks();
    });
    expect(apiDeleteCalls()).toBe(1);
    expect(keepaliveCalls()).toBe(1);
  });

  it('failed unmount flush of a confirmed delete surfaces an error', { timeout: 30_000 }, async () => {
    vi.mocked(deleteDocument).mockRejectedValue(new Error('delete rejected'));

    let utils!: ReturnType<typeof render>;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });

    await confirmSingleDelete();
    // Guard: the load was clean — no error toast so far.
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled();

    // Unmount INSIDE the undo window: the confirmed delete is flushed.
    await act(async () => {
      utils.unmount();
    });
    await act(async () => {
      await flushMicrotasks();
    });

    // Discriminating assertion: the flushed delete failed, so an error toast
    // must surface (at base the flush swallows the rejection silently).
    expect(vi.mocked(toast.error).mock.calls.length).toBeGreaterThan(0);
    // Message oracle (round 1): the toast names the failed action.
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      'Failed to delete document',
      expect.anything(),
    );
  });

  // Review round 2 (reviewer F4): the backend DELETE is not idempotent — a
  // replay 404 means the keepalive delete ALREADY landed, so the pageshow
  // replay must treat it as completion (success toast), never as failure.
  it('a pageshow replay 404 is completion, not failure', { timeout: 30_000 }, async () => {
    // First replay call rejects with the production normalized 404 shape;
    // (only one pagehide-dispatched id exists, so exactly one replay runs).
    vi.mocked(deleteDocument).mockRejectedValue(
      Object.assign(new Error('Request failed with status code 404'), { status: 404 }),
    );

    let utils!: ReturnType<typeof render>;
    await act(async () => {
      utils = render(<DocumentsPage />);
    });
    await confirmSingleDelete();
    await act(async () => {
      fireEvent(window, new Event('pagehide'));
    });
    // No credential in this suite: the keepalive channel deferred the id.
    expect(keepaliveCalls()).toBe(0);

    await act(async () => {
      const pageshow = new Event('pageshow');
      (pageshow as unknown as { persisted: boolean }).persisted = true;
      fireEvent(window, pageshow);
    });
    await act(async () => {
      await flushMicrotasks();
    });

    // Discriminating assertion: the 404 (delete already landed via the
    // keepalive dispatch in production) surfaces SUCCESS, and no error toast
    // fires — deleting this branch reintroduces the false failure toast.
    expect(vi.mocked(toast.success)).toHaveBeenCalledWith('Deleted document');
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled();
    await act(async () => {
      utils.unmount();
    });
  });

  // Review round 2, PRR-007 — useAuthStore.logout dispatches
  // "ragapp:before-logout" as its FIRST line, while credentials are still
  // valid. The listener runs the SPA-unmount-semantics flush (round 2
  // channel fix: a pagehide-style deferral could never replay — an SPA
  // logout fires no pageshow): the confirmed delete goes out via the normal
  // deleteDocument path; a later pageshow cannot re-send it.
  it('logout event flushes a pending delete before credentials clear', { timeout: 30_000 }, async () => {
    await act(async () => {
      render(<DocumentsPage />);
    });
    await confirmSingleDelete();
    expect(apiDeleteCalls()).toBe(0);

    await act(async () => {
      fireEvent(window, new Event('ragapp:before-logout'));
      await flushMicrotasks();
    });
    // Discriminating assertion: the delete went out via the NORMAL path.
    expect(apiDeleteCalls()).toBe(1);
    expect(keepaliveCalls()).toBe(0);

    // A persisted pageshow must not replay it (nothing was registered).
    const pageshow = new Event('pageshow');
    (pageshow as unknown as { persisted: boolean }).persisted = true;
    await act(async () => {
      fireEvent(window, pageshow);
      await flushMicrotasks();
    });
    expect(apiDeleteCalls()).toBe(1);
  });

  it('tag and folder failure logs carry no Authorization header', { timeout: 30_000 }, async () => {
    const authError = normalizedBearerError();
    // Sentinel: the fixture error serializes the Authorization header (via the AxiosError under originalError).
    expect(JSON.stringify(authError)).toContain('Authorization');
    vi.mocked(listTags).mockRejectedValue(authError);
    vi.mocked(listFolders).mockRejectedValue(authError);
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    await act(async () => {
      render(<DocumentsPage />);
    });
    // Sentinel: both failure fetches fired and both failure logs landed
    // (one per failing surface — the leak assertion below is only
    // meaningful if there IS a logged argument to leak).
    await waitFor(() => expect(vi.mocked(listTags)).toHaveBeenCalled(), { timeout: 10_000 });
    await waitFor(() => expect(vi.mocked(listFolders)).toHaveBeenCalled(), { timeout: 10_000 });
    await act(async () => {
      await flushMicrotasks();
    });
    expect(errorSpy.mock.calls.length).toBeGreaterThanOrEqual(2);

    const logged = serializedLoggedArgs([errorSpy, warnSpy]);
    const leaking = logged.filter((serialized) => serialized.includes('Authorization'));

    // Discriminating assertion: no logged argument may serialize the request
    // config's Authorization header. At base both console.error(err) calls
    // serialize the AxiosError with its config: 2 leaking arguments.
    // RED at base, GREEN after the fix.
    expect(leaking).toHaveLength(0);
    // Positive pins (mirroring C18): the sanitized log still DELIVERS the
    // status and URL — stripping the header must not strip the diagnosis.
    expect(logged.some((serialized) => serialized.includes('500'))).toBe(true);
    expect(logged.some((serialized) => serialized.includes('/documents'))).toBe(true);
  });
});
