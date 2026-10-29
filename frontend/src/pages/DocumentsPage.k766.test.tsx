// frontend/src/pages/DocumentsPage.k766.test.tsx
// K766 acceptance checks (issue #766, frozen) — confirmed-delete durability
// across the undo window (C12/C13) and Authorization-header leakage in
// tag/folder failure logs (C19).
//
//   C12 (DISCRIMINATING) — a pagehide fired INSIDE the 3s undo window must
//   still dispatch the confirmed delete through the keepalive channel when
//   a CSRF credential is cached, and — with no credential — dispatch
//   NOTHING and register the id for the bfcache pageshow replay. At base
//   pagehide does nothing. RED at base, GREEN after the fix.
//
//   C13 (DISCRIMINATING) — when that flush (unmount inside the undo window)
//   FAILS, an error must surface. At base the unmount flush swallows the
//   rejection silently. RED at base, GREEN after the fix.
//
//   C19 (DISCRIMINATING) — tag/folder failure logs must not serialize the
//   request config (and its Authorization header). At base the raw
//   AxiosError is logged. RED at base, GREEN after the fix.
//
// Amended after review round 1: C12 follows the NEW design (the sendBeacon
// fallback is gone — no production path uses it, so no stub here); the
// keepalive channel is pinned through the stubbed global fetch with its
// DELETE/keepalive/X-CSRF-Token shape, and the pageshow replay through
// deleteDocument (the keepalive outcome is unconfirmed, so a bfcache return
// completes the intent through the normal path). C13 pins the toast MESSAGE,
// and C19 adds an emission sentinel (>= 2 failure logs).
//
// Mock structure mirrors DocumentsPage.k03.test.tsx (proven in this repo):
// the full "@/lib/api" mock, the sonner callable toast mock, the
// useVaultStore/useUploadStore/useDebounce mocks, the UI-primitive stubs,
// the react-virtual stub, and the real ConfirmDialog (Radix portal).

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

// Import component after mocks
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
  // confirm BY ROLE inside the dialog (never by the literal button text).
  const dialog = await screen.findByRole('dialog', {}, { timeout: 10_000 });
  const confirmButton = within(dialog).getByRole('button', { name: /confirm|delete/i });
  expect(confirmButton).toBeEnabled();
  await act(async () => {
    fireEvent.click(confirmButton);
  });
  // Guard: the confirm dialog closed (the confirm actually happened).
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 5_000 });
}

/** A real AxiosError whose serialized config carries an Authorization header. */
function authorizationBearerError(): AxiosError {
  const config = {
    headers: { Authorization: 'Bearer probe-token' },
  } as unknown as InternalAxiosRequestConfig;
  return new AxiosError('boom', '500', config, {}, null);
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

  /** Count keepalive fetch calls for doc "1" (any method — shape asserted
   * separately in the credential test). */
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
    // no real request leaves jsdom (sendBeacon is gone from the design —
    // no production path uses it, so it is not stubbed here either).
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

    // Confirm one delete (starts the 3s undo window; the real delete is
    // deferred inside it). No CSRF credential is cached in this test env.
    await confirmSingleDelete();
    expect(vi.mocked(deleteDocument)).not.toHaveBeenCalled();

    // Leave the page WITHOUT unmounting: the tab is closing.
    await act(async () => {
      fireEvent(window, new Event('pagehide'));
    });

    // Discriminating assertion (first half): with NO credential the pagehide
    // dispatches NOTHING — neither the axios path (a CSRF-discovery round
    // trip the browser would abort mid-teardown) nor a keepalive fetch. The
    // id is registered for the bfcache replay instead. At base pagehide does
    // nothing at all, so this alone would pass there — the replay below is
    // what discriminates.
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
    // the registered delete through the normal path — exactly once.
    expect(apiDeleteCalls()).toBe(1);
  });

  it('pagehide with a cached CSRF credential issues the keepalive delete', { timeout: 30_000 }, async () => {
    // Seed the readable CSRF cookie the keepalive channel reads
    // (getCsrfCookie reads document.cookie; no sendBeacon involved).
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
    // fetch — exactly one request, with the destructive shape (DELETE,
    // keepalive, the cached CSRF header) and no axios deleteDocument call.
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

    // Bfcache return: the keepalive outcome is UNCONFIRMED (its response is
    // not observable), so the pageshow replay completes the intent through
    // the normal path — one deleteDocument call, success toast and all.
    const pageshow = new Event('pageshow');
    (pageshow as unknown as { persisted: boolean }).persisted = true;
    await act(async () => {
      fireEvent(window, pageshow);
    });
    await act(async () => {
      await flushMicrotasks();
    });
    expect(apiDeleteCalls()).toBe(1);

    // Exactly-once pin: the pagehide flush already emptied the pending
    // map, so the SPA-unmount flush finds nothing — no second call on
    // either channel.
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
    // must surface (the destructive action did NOT happen — the user must
    // know). At base the unmount flush swallows the rejection silently.
    // RED at base, GREEN after the fix.
    expect(vi.mocked(toast.error).mock.calls.length).toBeGreaterThan(0);
    // Message oracle (review round 1): the toast names the failed action —
    // not an unrelated or generic error that would pass vacuously.
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      'Failed to delete document',
      expect.anything(),
    );
  });

  it('tag and folder failure logs carry no Authorization header', { timeout: 30_000 }, async () => {
    const authError = authorizationBearerError();
    // Sentinel: the fixture error itself serializes the Authorization header
    // (a real AxiosError whose toJSON carries config.headers).
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

    const leaking = serializedLoggedArgs([errorSpy, warnSpy]).filter((serialized) =>
      serialized.includes('Authorization'),
    );

    // Discriminating assertion: no logged argument may serialize the request
    // config's Authorization header. At base both console.error(err) calls
    // serialize the AxiosError with its config: 2 leaking arguments.
    // RED at base, GREEN after the fix.
    expect(leaking).toHaveLength(0);
  });
});
