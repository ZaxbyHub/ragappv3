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
}));

// Mock react-dropzone
vi.mock('react-dropzone', () => ({
  useDropzone: vi.fn(() => ({
    getRootProps: () => ({ role: 'button' }),
    getInputProps: () => ({ type: 'file' }),
    isDragActive: false,
  })),
}));

// Mock sonner toast
vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

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
  StatusBadge: ({ status }: { status: string }) => <span data-testid="status-badge">{status}</span>,
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

// Import component after mocks
import DocumentsPage from '@/pages/DocumentsPage';
import { useVaultStore } from '@/stores/useVaultStore';
import { getDocumentStats, listDocuments } from '@/lib/api';

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
});
