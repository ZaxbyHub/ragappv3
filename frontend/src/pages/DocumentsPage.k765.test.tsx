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
import { render as rtlRender, act, waitFor, screen } from '@testing-library/react';
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

    // Discriminating assertion: an UNKNOWN wiki status (the fetch failed)
    // must not render the Compile affordance — that invites a compile we
    // cannot know is needed. At base the missing map entry is treated as
    // "not_compiled" and one Compile button renders.
    // RED at base, GREEN after the fix.
    expect(screen.queryAllByRole('button', { name: 'Compile' }).length).toBe(0);
  });
});
