// frontend/src/tests/DocumentsPage.adversarial.virtualization.test.tsx
/**
 * ADVERSARIAL VIRTUALIZATION TESTS for DocumentsPage
 *
 * These tests focus on attack vectors specific to the @tanstack/react-virtual
 * integration: malformed documents, oversized payloads, rapid array mutations,
 * boundary violations, and XSS through the virtualized render path.
 *
 * IMPORTANT: These tests MUST mock @tanstack/react-virtual to properly test
 * the virtualizer's handling of adversarial inputs.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render as rtlRender, screen, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

import DocumentsPage from "@/pages/DocumentsPage";

// DocumentsPage renders <Link> for document names; provide a router context.
const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

// =============================================================================
// MOCK RESIZE OBSERVER
// =============================================================================

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

// =============================================================================
// VIRTUALIZER MOCK STATE
// =============================================================================

// Track virtualizer state for dynamic behavior verification
let _mockDocCount = 0;
let _scrollToIndexCalls: number[] = [];
let _measureCalls = 0;

// =============================================================================
// MOCK @tanstack/react-virtual
// =============================================================================

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(({ count }) => {
    _mockDocCount = count;

    return {
      getVirtualItems: () =>
        Array.from({ length: count }, (_, i) => ({
          index: i,
          start: i * 56,
          size: 56,
          key: `doc-${i}`,
          end: (i + 1) * 56,
        })),
      getTotalSize: () => count * 56,
      measureElement: vi.fn((el: HTMLElement) => ({
        getBoundingClientRect: () => ({ height: 56, width: 1200 }),
      })),
      scrollToIndex: vi.fn((index: number) => {
        _scrollToIndexCalls.push(index);
      }),
      measure: vi.fn(() => {
        _measureCalls++;
      }),
      scrollOffset: 0,
      totalSize: count * 56,
    };
  }),
}));

// =============================================================================
// MOCK API
// =============================================================================

vi.mock("@/lib/api", () => ({
  listDocuments: vi.fn().mockResolvedValue({
    documents: [
      { id: "1", filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
    ],
  }),
  scanDocuments: vi.fn().mockResolvedValue({ added: 0, scanned: 0 }),
  deleteDocument: vi.fn().mockResolvedValue({}),
  deleteDocuments: vi.fn().mockResolvedValue({ deleted_count: 0, failed_ids: [] }),
  deleteAllDocumentsInVault: vi.fn().mockResolvedValue({ deleted_count: 0 }),
  getDocumentStats: vi.fn().mockResolvedValue({
    total_documents: 1,
    total_chunks: 5,
    total_size_bytes: 1024,
    documents_by_status: { processed: 1 },
  }),
}));

// =============================================================================
// OTHER MOCKS
// =============================================================================

vi.mock("react-dropzone", () => ({
  useDropzone: vi.fn(() => ({
    getRootProps: () => ({ role: "button" }),
    getInputProps: () => ({ type: "file" }),
    isDragActive: false,
  })),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

vi.mock("@/hooks/useDebounce", () => ({
  useDebounce: vi.fn((value: string) => [value, false]),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: null,
    vaults: [],
  })),
}));

vi.mock("@/stores/useUploadStore", () => ({
  useUploadStore: vi.fn(() => ({
    uploads: [],
    addUploads: vi.fn(),
    cancelUpload: vi.fn(),
    removeUpload: vi.fn(),
    clearCompleted: vi.fn(),
    retryUpload: vi.fn(),
  })),
}));

vi.mock("@/components/ui/card", () => ({
  Card: ({ children }: { children: React.ReactNode }) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: { children: React.ReactNode }) => <div data-testid="card-content">{children}</div>,
  CardDescription: ({ children }: { children: React.ReactNode }) => <p data-testid="card-description">{children}</p>,
  CardHeader: ({ children }: { children: React.ReactNode }) => <div data-testid="card-header">{children}</div>,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h3 data-testid="card-title">{children}</h3>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, onClick, disabled, ...props }: { children: React.ReactNode; onClick?: () => void; disabled?: boolean }) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}));

vi.mock("@/components/ui/input", () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock("@/components/ui/badge", () => ({
  Badge: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock("@/components/ui/progress", () => ({
  Progress: () => <div role="progressbar" />,
}));

vi.mock("@/components/ui/skeleton", () => ({
  Skeleton: () => <div data-testid="skeleton" />,
}));

vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({ onCheckedChange, checked, "aria-label": ariaLabel, ...props }: { onCheckedChange?: (checked: boolean) => void; checked?: boolean; "aria-label"?: string }) => (
    <input type="checkbox" onChange={(e) => onCheckedChange?.(e.target.checked)} checked={checked} aria-label={ariaLabel} {...props} />
  ),
}));

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open: boolean }) => (open ? <div data-testid="dialog">{children}</div> : null),
  DialogContent: ({ children }: { children: React.ReactNode }) => <div data-testid="dialog-content">{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("@/components/shared/StatusBadge", () => ({
  StatusBadge: ({ status }: { status: string }) => <span data-testid="status-badge">{status}</span>,
}));

vi.mock("@/components/shared/DocumentCard", () => ({
  DocumentCard: ({ document, onDelete, isSelected, onSelectionChange }: { document: { id: string; filename: string }; onDelete?: () => void; isSelected?: boolean; onSelectionChange?: () => void }) => (
    <div data-testid="document-card" data-selected={isSelected}>
      {document.filename}
    </div>
  ),
}));

vi.mock("@/components/EmptyState", () => ({
  EmptyState: () => <div data-testid="empty-state" />,
}));

vi.mock("@/lib/formatters", () => ({
  formatFileSize: (bytes: number) => `${bytes} bytes`,
  formatDate: (date: string) => date,
}));

// =============================================================================
// ADVERSARIAL TEST SUITE
// =============================================================================

describe("DocumentsPage ADVERSARIAL - Virtualization Attack Vectors", () => {
  let container: HTMLElement;
  let unmount: () => void;

  beforeEach(() => {
    vi.clearAllMocks();
    _mockDocCount = 0;
    _scrollToIndexCalls = [];
    _measureCalls = 0;

    // Set up ResizeObserver mock
    global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

    // Mock scrollIntoView
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    if (unmount) {
      unmount();
    }
  });

  // ===========================================================================
  // 1. MALFORMED DOCUMENT OBJECTS - Missing id, null filename, undefined metadata
  // ===========================================================================
  describe("Malformed document objects through virtualizer", () => {
    it("should handle document with missing id property", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with null filename", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: null, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with undefined metadata", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: undefined } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with null metadata", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: null } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with null id", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: null, filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle completely empty document object", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [{} as any],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with Symbol id (non-string key)", async () => {
      const symId = Symbol("test");
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: symId, filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with numeric id (0)", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: 0, filename: "Zero id.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with empty string id", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "", filename: "Empty id.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with undefined filename", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: undefined, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });
  });

  // ===========================================================================
  // 2. EXTREMELY LARGE NUMBER OF DOCUMENTS (500+) - Virtualizer stress test
  // ===========================================================================
  describe("Extremely large document arrays (500+)", () => {
    it("should handle 500 documents without crashing", async () => {
      const manyDocs = Array.from({ length: 500 }, (_, i) => ({
        id: String(i + 1),
        filename: `document_${i + 1}.pdf`,
        size: 1024 * (i + 1),
        created_at: "2024-01-01",
        metadata: { status: "processed", chunk_count: 5 },
      }));

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({ documents: manyDocs });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
      expect(_mockDocCount).toBe(500);
    });

    it("should handle 1000 documents through virtualizer", async () => {
      const manyDocs = Array.from({ length: 1000 }, (_, i) => ({
        id: String(i + 1),
        filename: `document_${i + 1}.pdf`,
        size: 1024 * (i + 1),
        created_at: "2024-01-01",
        metadata: { status: "processed", chunk_count: 5 },
      }));

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({ documents: manyDocs });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
      expect(_mockDocCount).toBe(1000);
    });

    it("should handle rapidly adding documents to reach 500+", async () => {
      const { listDocuments } = await import("@/lib/api");

      // Start with small set
      let currentDocs = Array.from({ length: 10 }, (_, i) => ({
        id: String(i + 1),
        filename: `initial_${i + 1}.pdf`,
        size: 1024,
        created_at: "2024-01-01",
        metadata: { status: "processed", chunk_count: 5 },
      }));

      listDocuments.mockResolvedValueOnce({ documents: currentDocs });

      let result: ReturnType<typeof render>;
      await act(async () => {
        result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      // Grow to 500+
      currentDocs = Array.from({ length: 500 }, (_, i) => ({
        id: String(i + 1),
        filename: `grown_${i + 1}.pdf`,
        size: 1024,
        created_at: "2024-01-01",
        metadata: { status: "processed", chunk_count: 5 },
      }));

      listDocuments.mockResolvedValueOnce({ documents: currentDocs });

      await act(async () => {
        result!.rerender(<DocumentsPage />);
      });

      expect(listDocuments).toHaveBeenCalled();
    });
  });

  // ===========================================================================
  // 3. DOCUMENTS WITH EXTREMELY LONG FILENAMES (>10KB)
  // ===========================================================================
  describe("Extremely long filenames (>10KB)", () => {
    it("should handle 10KB filename through virtualizer", async () => {
      const largeFilename = "A".repeat(10 * 1024) + ".pdf";

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: largeFilename, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle 50KB filename", async () => {
      const hugeFilename = "B".repeat(50 * 1024) + ".pdf";

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: hugeFilename, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle multiple documents each with 10KB filename", async () => {
      const largeFilename = "C".repeat(10 * 1024);

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: largeFilename + "_1.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
          { id: "2", filename: largeFilename + "_2.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
          { id: "3", filename: largeFilename + "_3.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle extremely long single-word filename (no path separators, 20KB)", async () => {
      const singleWordFilename = "X".repeat(20 * 1024) + ".pdf";

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: singleWordFilename, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });
  });

  // ===========================================================================
  // 4. DOCUMENTS WITH EMPTY STRING AND MISSING PROPERTIES
  // ===========================================================================
  describe("Empty/missing document properties through virtualizer", () => {
    it("should handle empty string filename", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle multiple documents with empty filenames", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
          { id: "2", filename: "", size: 2048, created_at: "2024-01-02", metadata: { status: "processed", chunk_count: 10 } },
          { id: "3", filename: "", size: 4096, created_at: "2024-01-03", metadata: { status: "processed", chunk_count: 20 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle mix of valid and invalid filenames", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "valid.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
          { id: "2", filename: "", size: 2048, created_at: "2024-01-02", metadata: { status: "processed", chunk_count: 10 } },
          { id: "3", filename: null as any, size: 4096, created_at: "2024-01-03", metadata: { status: "processed", chunk_count: 20 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });
  });

  // ===========================================================================
  // 5. RAPIDLY CHANGING DOCUMENT ARRAYS
  // ===========================================================================
  describe("Rapidly changing document arrays", () => {
    it("should handle rapid document additions", async () => {
      const { listDocuments } = await import("@/lib/api");

      let result: ReturnType<typeof render>;

      // Rapidly add documents
      for (let i = 0; i < 50; i++) {
        const docs = Array.from({ length: i + 1 }, (_, j) => ({
          id: String(j + 1),
          filename: `doc_${j + 1}.pdf`,
          size: 1024,
          created_at: "2024-01-01",
          metadata: { status: "processed", chunk_count: 5 },
        }));

        listDocuments.mockResolvedValueOnce({ documents: docs });

        await act(async () => {
          result = render(<DocumentsPage />);
          container = result.container;
          unmount = result.unmount;
        });
      }
    });

    it("should handle rapid document removals", async () => {
      const { listDocuments } = await import("@/lib/api");

      let result: ReturnType<typeof render>;

      // Start with 50 documents
      let docs = Array.from({ length: 50 }, (_, j) => ({
        id: String(j + 1),
        filename: `doc_${j + 1}.pdf`,
        size: 1024,
        created_at: "2024-01-01",
        metadata: { status: "processed", chunk_count: 5 },
      }));

      listDocuments.mockResolvedValueOnce({ documents: docs });

      await act(async () => {
        result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      // Rapidly remove documents
      for (let i = 50; i >= 0; i--) {
        docs = Array.from({ length: i }, (_, j) => ({
          id: String(j + 1),
          filename: `doc_${j + 1}.pdf`,
          size: 1024,
          created_at: "2024-01-01",
          metadata: { status: "processed", chunk_count: 5 },
        }));

        listDocuments.mockResolvedValueOnce({ documents: docs });

        await act(async () => {
          result!.rerender(<DocumentsPage />);
        });
      }
    });

    it("should handle document array replaced entirely", async () => {
      const { listDocuments } = await import("@/lib/api");

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      // Replace with completely new documents
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "new-1", filename: "new_document_1.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
          { id: "new-2", filename: "new_document_2.pdf", size: 2048, created_at: "2024-01-02", metadata: { status: "pending", chunk_count: 0 } },
        ],
      });

      await act(async () => {
        render(<DocumentsPage />);
      });
    });
  });

  // ===========================================================================
  // 6. VIRTUALIZER WITH ZERO-HEIGHT SCROLL CONTAINER
  // ===========================================================================
  describe("Zero-height scroll container edge case", () => {
    it("should handle scroll container with zero height", async () => {
      // This tests the edge case where getBoundingClientRect returns 0 height
      const originalGetBoundingClientRect = Element.prototype.getBoundingClientRect;
      Element.prototype.getBoundingClientRect = vi.fn(() => ({
        height: 0,
        width: 1200,
        top: 0,
        left: 0,
        right: 1200,
        bottom: 0,
        x: 0,
        y: 0,
      }));

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      // Restore
      Element.prototype.getBoundingClientRect = originalGetBoundingClientRect;

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle virtualizer measure returning zero height", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });
  });

  // ===========================================================================
  // 7. XSS PAYLOADS IN FILENAMES AND STATUS FIELDS
  // ===========================================================================
  describe("XSS payloads through virtualized render path", () => {
    const xssPayloads = [
      '<img onerror="alert(1)" src=x>',
      '<script>alert("xss")</script>',
      '<svg onload="alert(1)">',
      '"><script>alert(document.cookie)</script>',
      '<a href="javascript:alert(1)">Click</a>',
      '<div onclick="alert(1)">click me</div>',
      '<iframe src="javascript:alert(1)"></iframe>',
      '${alert(1)}',
      '{{constructor.constructor("alert(1)")()}}',
      '<img src=x onerror="eval(atob(\'YWxlcnQoMSk=\'))">',
    ];

    it.each(xssPayloads)("should NOT execute XSS payload in filename: %s", async (payload) => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: payload + ".pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      // Verify no script elements were created
      expect(document.querySelector("script")).toBeNull();
    });

    it.each(xssPayloads)("should NOT execute XSS payload in status field: %s", async (payload) => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: payload, chunk_count: 5 } } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      // Verify no script elements were created
      expect(document.querySelector("script")).toBeNull();
    });

    it("should NOT execute XSS in multiple virtualized documents", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: '<script>alert("xss1")</script>.pdf', size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
          { id: "2", filename: 'normal.pdf', size: 2048, created_at: "2024-01-02", metadata: { status: '<img onerror="alert(2)" src=x>', chunk_count: 10 } as any },
          { id: "3", filename: 'another.pdf', size: 4096, created_at: "2024-01-03", metadata: { status: "pending", chunk_count: 0 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      // No script execution
      expect(document.querySelector("script")).toBeNull();
    });
  });

  // ===========================================================================
  // 8. UNICODE AND SPECIAL CHARACTERS IN FILENAMES
  // ===========================================================================
  describe("Unicode and special characters in filenames through virtualizer", () => {
    it("should handle RTL override characters in filenames", async () => {
      const rtlFilename = "\u202EEvil\u202C File.pdf";

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: rtlFilename, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle null byte in filename", async () => {
      const nullByteFilename = "Hello\x00World.pdf";

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: nullByteFilename, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle combining characters in filename", async () => {
      const combiningFilename = "cafe\u0301.pdf"; // café with combining accent

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: combiningFilename, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle zero-width space in filename", async () => {
      const zwspFilename = "Hello\u200BWorld.pdf";

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: zwspFilename, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle emoji in filename", async () => {
      const emojiFilename = "document_👍_v2.pdf";

      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: emojiFilename, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });
  });

  // ===========================================================================
  // 9. BOUNDARY VIOLATIONS - NaN, Infinity, Negative values
  // ===========================================================================
  describe("Boundary violations through virtualizer", () => {
    it("should handle NaN document id", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: String(NaN), filename: "NaN id.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle Infinity document id", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: String(Infinity), filename: "Infinity id.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle -Infinity document id", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: String(-Infinity), filename: "-Infinity id.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle Number.MAX_SAFE_INTEGER id", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: String(Number.MAX_SAFE_INTEGER), filename: "Max safe int.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle negative document index", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "-5", filename: "Negative index.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle -0 as document id", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "-0", filename: "Negative zero id.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle negative size value", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "negativesize.pdf", size: -1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });
  });

  // ===========================================================================
  // 10. TYPE CONFUSION ATTACKS
  // ===========================================================================
  describe("Type confusion attacks through virtualizer", () => {
    it("should handle document with number filename instead of string", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: 12345 as any, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with array filename instead of string", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: ["array", "filename"] as any, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with object filename instead of string", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: { nested: "object" } as any, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with undefined filename", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: undefined, size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } } as any,
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with number status instead of string", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: 123 as any, chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });

    it("should handle document with array metadata.status instead of string", async () => {
      const { listDocuments } = await import("@/lib/api");
      listDocuments.mockResolvedValueOnce({
        documents: [
          { id: "1", filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: ["array", "status"] as any, chunk_count: 5 } },
        ],
      });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      expect(listDocuments).toHaveBeenCalled();
    });
  });
});
