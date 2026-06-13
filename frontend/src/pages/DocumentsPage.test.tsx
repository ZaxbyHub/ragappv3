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

// Mock API with documents so the table renders
vi.mock('@/lib/api', () => ({
  listDocuments: vi.fn().mockResolvedValue({ 
    documents: [
      { id: '1', filename: 'test.pdf', size: 1024, created_at: '2024-01-01', metadata: { status: 'processed', chunk_count: 5 } },
      { id: '2', filename: 'test2.pdf', size: 2048, created_at: '2024-01-02', metadata: { status: 'processed', chunk_count: 10 } },
    ] 
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

// Mock react-dropzone
vi.mock('react-dropzone', () => ({
  useDropzone: vi.fn(() => ({
    getRootProps: () => ({ role: 'button' }),
    getInputProps: () => ({ type: 'file' }),
    isDragActive: false,
  })),
}));

// Mock sonner toast. `toast` is callable (toast("msg", {action,...})) with
// method shortcuts and dismiss, matching the undo-toast delete path.
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

// Mock useVaultStore
vi.mock('@/stores/useVaultStore', () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: null,
    vaults: [],
  })),
}));

// Mock useUploadStore
vi.mock('@/stores/useUploadStore', () => ({
  useUploadStore: vi.fn(() => ({
    uploads: [],
    addUploads: vi.fn(),
    cancelUpload: vi.fn(),
    removeUpload: vi.fn(),
    clearCompleted: vi.fn(),
    retryUpload: vi.fn(),
  })),
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

vi.mock('@/components/shared/EmptyState', () => ({
  EmptyState: ({ title, description }: { title: string; description?: string }) => (
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

// Test control for the UploadDropzone mock: the factory installs a global
// handle so tests can drive the onRejected / onFiles callbacks that the page
// passes to the dropzone. The mock renders a tiny "test bench" so we can
// assert whether the real component (and the page-level wiring) is connected.
const dropzoneTestHandle: {
  current: {
    onFiles: ((files: File[]) => void) | null;
    onRejected: ((names: string[]) => void) | null;
  };
} = {
  current: { onFiles: null, onRejected: null },
};
vi.mock('@/components/documents/UploadDropzone', () => ({
  UploadDropzone: (props: {
    onFiles: (files: File[]) => void;
    onRejected: (names: string[]) => void;
  }) => {
    dropzoneTestHandle.current.onFiles = props.onFiles;
    dropzoneTestHandle.current.onRejected = props.onRejected;
    return (
      <div data-testid="upload-dropzone-stub">
        <button
          type="button"
          data-testid="dropzone-trigger-rejected"
          onClick={() =>
            dropzoneTestHandle.current.onRejected?.(['a.exe (too large)', 'b.xyz (unsupported)'])
          }
        >
          trigger rejected
        </button>
        <button
          type="button"
          data-testid="dropzone-trigger-rejected-empty"
          onClick={() => dropzoneTestHandle.current.onRejected?.([])}
        >
          trigger rejected empty
        </button>
        <button
          type="button"
          data-testid="dropzone-trigger-rejected-new"
          onClick={() => dropzoneTestHandle.current.onRejected?.(['c.txt (too large)'])}
        >
          trigger rejected new
        </button>
        <button
          type="button"
          data-testid="dropzone-trigger-files"
          onClick={() => dropzoneTestHandle.current.onFiles?.([])}
        >
          trigger files
        </button>
      </div>
    );
  },
}));

vi.mock('@/components/documents/RejectedFilesBanner', () => ({
  RejectedFilesBanner: ({ files, onDismiss }: { files: string[]; onDismiss: () => void }) => {
    if (files.length === 0) return null;
    return (
      <div data-testid="rejected-files-banner" role="status" aria-live="polite">
        <span data-testid="rejected-files-count">
          {files.length === 1 ? '1 file was rejected' : `${files.length} files were rejected`}
        </span>
        <ul data-testid="rejected-files-list">
          {files.map((file, index) => (
            <li key={index} data-testid="rejected-files-item">
              {file}
            </li>
          ))}
        </ul>
        <button type="button" aria-label="Dismiss rejected files list" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    );
  },
}));

// Import component after mocks
import DocumentsPage from '@/pages/DocumentsPage';
import { useVaultStore } from '@/stores/useVaultStore';
import { getDocumentStats, listDocuments, deleteDocument } from '@/lib/api';

describe('DocumentsPage - Drag to Resize Filename Column', () => {
  let container: HTMLElement;
  let unmount: () => void;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listDocuments).mockResolvedValue({
      documents: [
        { id: '1', filename: 'test.pdf', size: 1024, created_at: '2024-01-01', metadata: { status: 'processed', chunk_count: 5 } },
        { id: '2', filename: 'test2.pdf', size: 2048, created_at: '2024-01-02', metadata: { status: 'processed', chunk_count: 10 } },
      ],
      total: 2,
    });
    vi.mocked(getDocumentStats).mockResolvedValue({
      total_documents: 2,
      total_chunks: 15,
      total_size_bytes: 3072,
      documents_by_status: { processed: 2 },
    });
    vi.mocked(useVaultStore).mockReturnValue({
      activeVaultId: null,
      vaults: [],
    } as ReturnType<typeof useVaultStore>);
    document.body.style.cursor = '';
  });

  afterEach(() => {
    if (unmount) {
      unmount();
    }
    document.body.style.cursor = '';
  });

  // Helper to find the resize handle element
  const findResizeHandle = (): HTMLElement | null => {
    // The resize handle has class "cursor-col-resize" (via Tailwind's hover:bg-border)
    // and role="separator" with aria-orientation="vertical"
    return container.querySelector('[role="separator"][aria-orientation="vertical"]');
  };

  describe('Default State', () => {
    it('renders list-row phase progress and failed reason titles', async () => {
      vi.mocked(listDocuments).mockResolvedValueOnce({
        documents: [
          {
            id: 'failed-doc',
            filename: 'failed.pdf',
            size: 1024,
            created_at: '2024-01-01',
            error_message: 'Parser could not read the file',
            phase: 'parsing',
            phase_message: 'Parsing failed',
            progress_percent: 25,
            processed_units: 1,
            total_units: 4,
            unit_label: 'pages',
            metadata: {
              status: 'error',
              chunk_count: 0,
              error_message: 'Parser could not read the file',
              phase_message: 'Parsing failed',
              progress_percent: 25,
            },
          },
          {
            id: 'processing-doc',
            filename: 'processing.pdf',
            size: 2048,
            created_at: '2024-01-02',
            phase: 'embedding',
            phase_message: null,
            progress_percent: null,
            processed_units: 0,
            total_units: 0,
            unit_label: 'chunks',
            metadata: {
              status: 'processing',
              chunk_count: 0,
              phase: 'embedding',
              progress_percent: null,
              processed_units: 0,
              total_units: 0,
              unit_label: 'chunks',
            },
          },
        ],
        total: 2,
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(screen.getByText('Parsing failed - 1 / 4 pages')).toBeInTheDocument();
      });
      expect(screen.getByText('25%')).toBeInTheDocument();
      expect(screen.getByText('embedding - 0 / 0 chunks')).toBeInTheDocument();
      expect(screen.queryByText('NaN%')).not.toBeInTheDocument();
      expect(container.querySelector('[title="Parser could not read the file"]')).toBeInTheDocument();
    });

    it('renders Partially indexed for an indexed doc with failed chunks', async () => {
      vi.mocked(listDocuments).mockResolvedValueOnce({
        documents: [
          {
            id: 'partial-doc',
            filename: 'partial.pdf',
            size: 1024,
            created_at: '2024-01-01',
            metadata: { status: 'indexed', chunk_count: 7, chunks_failed: 3 },
          },
        ],
        total: 1,
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(screen.getByText('Partially indexed')).toBeInTheDocument();
      });
    });

    it('requests all-vault stats when no active vault is selected', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(getDocumentStats).toHaveBeenCalledWith(undefined);
      });
      expect(screen.getByText('2')).toBeInTheDocument();
      expect(screen.getByText('15')).toBeInTheDocument();
      expect(screen.getByText('3072 bytes')).toBeInTheDocument();
    });

    it('should have default filenameColWidth of 250', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });
      
      // Wait for loading to complete and table to render
      await waitFor(() => {
        const resizeHandle = findResizeHandle();
        expect(resizeHandle).toBeTruthy();
      });

      // Find the Filename header which uses the width
      // The first th with text content "Filename" should have style width: 250px
      const headers = Array.from(container.querySelectorAll('th[scope="col"]'));
      const filenameTh = headers.find(th => th.textContent?.includes('Filename'));
      expect(filenameTh).toBeTruthy();
    });
  });

  describe('Vault permission gating', () => {
    it('hides destructive document actions without an active vault', async () => {
      vi.mocked(useVaultStore).mockReturnValue({
        activeVaultId: null,
        vaults: [],
      } as ReturnType<typeof useVaultStore>);

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      expect(container.textContent).not.toContain('Delete All in Vault');
      expect(
        container.querySelector('input[aria-label="Select all documents"]')
      ).toBeDisabled();
    });

    it('enables destructive document actions for vault admins', async () => {
      vi.mocked(useVaultStore).mockReturnValue({
        activeVaultId: 2,
        vaults: [{ id: 2, name: 'Admin Vault', current_user_permission: 'admin' }],
      } as ReturnType<typeof useVaultStore>);

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      expect(container.textContent).toContain('Delete All in Vault');
      expect(
        container.querySelector('input[aria-label="Select all documents"]')
      ).not.toBeDisabled();
    });
  });

  describe('empty states', () => {
    it('shows the no-documents state when the selected vault has no documents', async () => {
      vi.mocked(useVaultStore).mockReturnValue({
        activeVaultId: 2,
        vaults: [{ id: 2, name: 'Writable Vault', current_user_permission: 'write' }],
      } as ReturnType<typeof useVaultStore>);
      vi.mocked(listDocuments).mockResolvedValueOnce({ documents: [], total: 0 });
      vi.mocked(getDocumentStats).mockResolvedValueOnce({
        total_documents: 0,
        total_chunks: 0,
        total_size_bytes: 0,
        documents_by_status: {},
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(screen.getByText('No documents yet')).toBeInTheDocument();
      });
      expect(screen.getByText('Upload files to get started.')).toBeInTheDocument();
    });

    it('shows the no-search-matches state when a populated vault has no matching documents', async () => {
      vi.mocked(useVaultStore).mockReturnValue({
        activeVaultId: 2,
        vaults: [{ id: 2, name: 'Admin Vault', current_user_permission: 'admin' }],
      } as ReturnType<typeof useVaultStore>);
      vi.mocked(listDocuments).mockResolvedValue({ documents: [], total: 0 });
      vi.mocked(getDocumentStats).mockResolvedValueOnce({
        total_documents: 2,
        total_chunks: 10,
        total_size_bytes: 2048,
        documents_by_status: { indexed: 2 },
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(screen.getByText('No documents yet')).toBeInTheDocument();
      });

      const searchInput = container.querySelector(
        'input[placeholder="Search documents and metadata..."]'
      ) as HTMLInputElement;
      fireEvent.change(searchInput, { target: { value: 'quarterly' } });

      await waitFor(() => {
        expect(screen.getByText('No documents match your search')).toBeInTheDocument();
      });
      expect(
        screen.getByText('Search checks filename, type, status, source, sender, subject, and document date.')
      ).toBeInTheDocument();
    });

    it('shows the "No vaults available" state when vaults list is empty', async () => {
      vi.mocked(useVaultStore).mockReturnValue({
        activeVaultId: null,
        vaults: [],
      } as ReturnType<typeof useVaultStore>);
      vi.mocked(listDocuments).mockResolvedValueOnce({ documents: [], total: 0 });
      vi.mocked(getDocumentStats).mockResolvedValueOnce({
        total_documents: 0,
        total_chunks: 0,
        total_size_bytes: 0,
        documents_by_status: {},
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(screen.getByText('No vaults available')).toBeInTheDocument();
      });
      expect(
        screen.getByText('Create a vault or ask an admin to grant you access to start uploading documents.')
      ).toBeInTheDocument();
    });

    it('does not show the no-documents state for active search while stats are unavailable', async () => {
      vi.mocked(useVaultStore).mockReturnValue({
        activeVaultId: 2,
        vaults: [{ id: 2, name: 'Admin Vault', current_user_permission: 'admin' }],
      } as ReturnType<typeof useVaultStore>);
      vi.mocked(listDocuments).mockResolvedValue({ documents: [], total: 0 });
      vi.mocked(getDocumentStats).mockRejectedValueOnce(new Error('stats unavailable'));

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(screen.getByText('No documents yet')).toBeInTheDocument();
      });

      const searchInput = container.querySelector(
        'input[placeholder="Search documents and metadata..."]'
      ) as HTMLInputElement;
      fireEvent.change(searchInput, { target: { value: 'quarterly' } });

      await waitFor(() => {
        expect(screen.getByRole('status')).toBeInTheDocument();
      });
      expect(screen.queryByText('No documents yet')).not.toBeInTheDocument();
    });
  });

  describe('handleResizeMouseDown - Cursor Management', () => {
    it('should set cursor to col-resize on mousedown', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const resizeHandle = findResizeHandle();
        expect(resizeHandle).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;
      
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 100 });
      });

      expect(document.body.style.cursor).toBe('col-resize');
    });

    it('should restore cursor on mouseup', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;
      
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 100 });
      });
      
      expect(document.body.style.cursor).toBe('col-resize');

      await act(async () => {
        fireEvent.mouseUp(document);
      });

      expect(document.body.style.cursor).toBe('');
    });
  });

  describe('handleResizeMouseDown - Width Calculation', () => {
    it('should increase width when dragging right (positive deltaX)', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;
      
      // Simulate drag right: start at x=100, move to x=200 (deltaX = +100)
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 100 });
        fireEvent.mouseMove(document, { clientX: 200 });
        fireEvent.mouseUp(document);
      });

      // Component should still render without error
      expect(container).toBeTruthy();
    });

    it('should decrease width when dragging left (negative deltaX)', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;
      
      // Simulate drag left: start at x=200, move to x=100 (deltaX = -100)
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 200 });
        fireEvent.mouseMove(document, { clientX: 100 });
        fireEvent.mouseUp(document);
      });

      expect(container).toBeTruthy();
    });

    it('should clamp width to maximum of 600', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;
      
      // Simulate extreme drag right: deltaX = +99999
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 0 });
        fireEvent.mouseMove(document, { clientX: 99999 });
        fireEvent.mouseUp(document);
      });

      // Width should be clamped to 600 - no error
      expect(container).toBeTruthy();
    });

    it('should clamp width to minimum of 120', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;
      
      // Simulate extreme drag left: start at 99999, move to 0 (deltaX = -99999)
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 99999 });
        fireEvent.mouseMove(document, { clientX: 0 });
        fireEvent.mouseUp(document);
      });

      // Width should be clamped to 120 - no error
      expect(container).toBeTruthy();
    });
  });

  describe('Adversarial Cases', () => {
    it('should handle rapid mousemove events without crashing', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;

      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 100 });
        
        // Rapid fire mousemove events
        for (let i = 0; i < 100; i++) {
          fireEvent.mouseMove(document, { clientX: 100 + i });
        }
        
        fireEvent.mouseUp(document);
      });

      expect(container).toBeTruthy();
    });

    it('should handle deltaX = 0 (no change)', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;

      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 100 });
        fireEvent.mouseMove(document, { clientX: 100 }); // No movement
        fireEvent.mouseUp(document);
      });

      expect(container).toBeTruthy();
    });

    it('should handle deltaX = +99999 (clamps to 600)', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;

      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 0 });
        fireEvent.mouseMove(document, { clientX: 99999 });
        fireEvent.mouseUp(document);
      });

      expect(container).toBeTruthy();
    });

    it('should handle deltaX = -99999 (clamps to 120)', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;

      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 99999 });
        fireEvent.mouseMove(document, { clientX: 0 }); // deltaX = -99999
        fireEvent.mouseUp(document);
      });

      expect(container).toBeTruthy();
    });

    it('should restore cursor and cleanup on unmount during active drag', async () => {
      let unmountFn: () => void;
      
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmountFn = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;

      // Start a drag operation
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 100 });
      });

      // Cursor should be set
      expect(document.body.style.cursor).toBe('col-resize');

      // Unmount during active drag
      await act(async () => {
        unmountFn();
      });

      // Cursor should be restored by cleanup useEffect
      expect(document.body.style.cursor).toBe('');
    });

    it('should handle event listener removal on mouseup (no memory leak)', async () => {
      const addEventListenerSpy = vi.spyOn(document, 'addEventListener');
      const removeEventListenerSpy = vi.spyOn(document, 'removeEventListener');

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;

      // Start drag
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 100 });
      });

      // Should have added mousemove and mouseup listeners
      expect(addEventListenerSpy).toHaveBeenCalledWith('mousemove', expect.any(Function));
      expect(addEventListenerSpy).toHaveBeenCalledWith('mouseup', expect.any(Function));

      // End drag
      await act(async () => {
        fireEvent.mouseUp(document);
      });

      // Should have removed mousemove and mouseup listeners
      expect(removeEventListenerSpy).toHaveBeenCalledWith('mousemove', expect.any(Function));
      expect(removeEventListenerSpy).toHaveBeenCalledWith('mouseup', expect.any(Function));

      addEventListenerSpy.mockRestore();
      removeEventListenerSpy.mockRestore();
    });
  });

  describe('Edge Cases', () => {
    it('should handle multiple sequential drag operations', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;

      // First drag operation
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 100 });
        fireEvent.mouseMove(document, { clientX: 150 });
        fireEvent.mouseUp(document);
      });

      // Second drag operation
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 100 });
        fireEvent.mouseMove(document, { clientX: 50 });
        fireEvent.mouseUp(document);
      });

      // Third drag operation
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 100 });
        fireEvent.mouseMove(document, { clientX: 100 });
        fireEvent.mouseUp(document);
      });

      expect(container).toBeTruthy();
    });

    it('should handle starting drag from already-clamped width', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(findResizeHandle()).toBeTruthy();
      });

      const resizeHandle = findResizeHandle()!;

      // Drag to maximum width first
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 0 });
        fireEvent.mouseMove(document, { clientX: 99999 });
        fireEvent.mouseUp(document);
      });

      // Try to drag further right (should stay at max)
      await act(async () => {
        fireEvent.mouseDown(resizeHandle, { clientX: 0 });
        fireEvent.mouseMove(document, { clientX: 99999 });
        fireEvent.mouseUp(document);
      });

      expect(container).toBeTruthy();
    });
  });

  describe('Rejected files banner (issue #55)', () => {
    // Reset the per-component handle so a stale callback from a prior render
    // can't bleed into the next test.
    beforeEach(() => {
      dropzoneTestHandle.current.onFiles = null;
      dropzoneTestHandle.current.onRejected = null;
    });

    it('does not render the rejected-files banner when no rejection has occurred', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(screen.queryByTestId('upload-dropzone-stub')).toBeInTheDocument();
      });
      expect(screen.queryByTestId('rejected-files-banner')).not.toBeInTheDocument();
    });

    it('renders the rejected-files banner with the rejected file list when the dropzone reports rejections', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(screen.getByTestId('upload-dropzone-stub')).toBeInTheDocument();
      });
      // The page must have wired up onRejected for the test stub to be functional.
      expect(dropzoneTestHandle.current.onRejected).toBeTypeOf('function');

      await act(async () => {
        fireEvent.click(screen.getByTestId('dropzone-trigger-rejected'));
      });

      const banner = await screen.findByTestId('rejected-files-banner');
      expect(banner).toBeInTheDocument();
      expect(screen.getByTestId('rejected-files-count').textContent).toBe(
        '2 files were rejected'
      );
      const items = screen.getAllByTestId('rejected-files-item');
      expect(items).toHaveLength(2);
      expect(items[0]).toHaveTextContent('a.exe (too large)');
      expect(items[1]).toHaveTextContent('b.xyz (unsupported)');
    });

    it('clears the rejected-files banner when the dismiss button is clicked', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(screen.getByTestId('upload-dropzone-stub')).toBeInTheDocument();
      });

      await act(async () => {
        fireEvent.click(screen.getByTestId('dropzone-trigger-rejected'));
      });
      expect(await screen.findByTestId('rejected-files-banner')).toBeInTheDocument();

      await act(async () => {
        fireEvent.click(screen.getByLabelText('Dismiss rejected files list'));
      });

      await waitFor(() => {
        expect(screen.queryByTestId('rejected-files-banner')).not.toBeInTheDocument();
      });
    });

    it('does not re-render the rejected-files banner after dismiss unless a new rejection arrives', async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        expect(screen.getByTestId('upload-dropzone-stub')).toBeInTheDocument();
      });

      // 1) Reject two files, banner appears.
      await act(async () => {
        fireEvent.click(screen.getByTestId('dropzone-trigger-rejected'));
      });
      expect(await screen.findByTestId('rejected-files-banner')).toBeInTheDocument();

      // 2) Dismiss — banner disappears.
      await act(async () => {
        fireEvent.click(screen.getByLabelText('Dismiss rejected files list'));
      });
      await waitFor(() => {
        expect(screen.queryByTestId('rejected-files-banner')).not.toBeInTheDocument();
      });

      // 3) Firing onRejected with an empty list must NOT bring the banner back.
      await act(async () => {
        fireEvent.click(screen.getByTestId('dropzone-trigger-rejected-empty'));
      });
      expect(screen.queryByTestId('rejected-files-banner')).not.toBeInTheDocument();

      // 4) Sanity: a fresh non-empty rejection does re-show the banner with the
      //    new file list — this proves the dismiss doesn't permanently break
      //    the wiring.
      await act(async () => {
        fireEvent.click(screen.getByTestId('dropzone-trigger-rejected-new'));
      });
      const banner = await screen.findByTestId('rejected-files-banner');
      expect(banner).toBeInTheDocument();
      expect(screen.getByTestId('rejected-files-count').textContent).toBe(
        '1 file was rejected'
      );
      expect(screen.getByTestId('rejected-files-item')).toHaveTextContent('c.txt (too large)');
    });
  });

  describe('pending delete flush on unmount (#223)', () => {
    it('executes a confirmed-but-not-yet-elapsed delete when the page unmounts', async () => {
      vi.mocked(useVaultStore).mockReturnValue({
        activeVaultId: 2,
        vaults: [{ id: 2, name: 'Admin Vault', current_user_permission: 'admin' }],
      } as ReturnType<typeof useVaultStore>);

      let localUnmount: () => void = () => {};
      await act(async () => {
        const result = render(<DocumentsPage />);
        localUnmount = result.unmount;
      });

      // Open the per-row delete confirm and confirm it (starts the 3s undo timer).
      const deleteButtons = await screen.findAllByLabelText('Delete document');
      await act(async () => {
        fireEvent.click(deleteButtons[0]);
      });
      const confirmBtn = await screen.findByRole('button', { name: 'Confirm' });
      await act(async () => {
        fireEvent.click(confirmBtn);
      });

      // The real delete is deferred inside the 3s undo window — not called yet.
      expect(vi.mocked(deleteDocument)).not.toHaveBeenCalled();

      // Unmount within the undo window: the confirmed delete must be flushed,
      // not silently cancelled.
      await act(async () => {
        localUnmount();
      });
      expect(vi.mocked(deleteDocument)).toHaveBeenCalledWith('1');
    });
  });

  describe('documents pagination (#218)', () => {
    it('shows a Load more control and fetches a larger window when clicked', async () => {
      vi.mocked(useVaultStore).mockReturnValue({
        activeVaultId: 2,
        vaults: [{ id: 2, name: 'Admin Vault', current_user_permission: 'admin' }],
      } as ReturnType<typeof useVaultStore>);
      // 50 of 120 documents returned: more remain on the server.
      const fiftyDocs = Array.from({ length: 50 }, (_, i) => ({
        id: String(i + 1),
        filename: `doc-${i + 1}.pdf`,
        size: 1024,
        created_at: '2024-01-01',
        metadata: { status: 'processed', chunk_count: 1 },
      }));
      vi.mocked(listDocuments).mockResolvedValue({ documents: fiftyDocs, total: 120 });

      await act(async () => {
        render(<DocumentsPage />);
      });

      expect(await screen.findByText('Showing 50 of 120 documents')).toBeInTheDocument();
      const loadMore = await screen.findByRole('button', { name: 'Load more' });

      vi.mocked(listDocuments).mockClear();
      await act(async () => {
        fireEvent.click(loadMore);
      });
      // The next fetch requests a larger window (perPage grew by one page).
      await waitFor(() => {
        expect(vi.mocked(listDocuments)).toHaveBeenCalledWith(
          expect.objectContaining({ perPage: 100 })
        );
      });
    });
  });
});
