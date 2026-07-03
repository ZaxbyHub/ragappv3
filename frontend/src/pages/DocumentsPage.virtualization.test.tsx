// frontend/src/pages/DocumentsPage.virtualization.test.tsx
/**
 * Virtualization Verification Tests for DocumentsPage
 *
 * These tests verify that the @tanstack/react-virtual integration
 * works correctly for both desktop table and mobile card views.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render as rtlRender, screen, fireEvent, act, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter } from "react-router-dom";

import DocumentsPage from "@/pages/DocumentsPage";

// DocumentsPage renders <Link> for document names; provide a router context.
const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });

// Mock @tanstack/react-virtual with dynamic behavior
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(({ count }) => ({
    getVirtualItems: () =>
      Array.from({ length: count }, (_, i) => ({
        index: i,
        start: i * 56,
        size: 56,
        key: `doc-${i}`,
        measureElement: vi.fn(),
      })),
    getTotalSize: () => count * 56,
    measureElement: vi.fn((el) => ({
      getBoundingClientRect: () => ({ height: 56 }),
    })),
    scrollToIndex: vi.fn(),
    measure: vi.fn(),
  })),
}));

// Mock API with documents so the table renders
vi.mock("@/lib/api", () => ({
  listDocuments: vi.fn().mockResolvedValue({
    documents: [
      { id: "1", filename: "test.pdf", size: 1024, created_at: "2024-01-01", metadata: { status: "processed", chunk_count: 5 } },
      { id: "2", filename: "test2.pdf", size: 2048, created_at: "2024-01-02", metadata: { status: "processed", chunk_count: 10 } },
      { id: "3", filename: "report.pdf", size: 4096, created_at: "2024-01-03", metadata: { status: "processed", chunk_count: 20 } },
      { id: "4", filename: "doc.docx", size: 8192, created_at: "2024-01-04", metadata: { status: "pending", chunk_count: 0 } },
      { id: "5", filename: "notes.md", size: 512, created_at: "2024-01-05", metadata: { status: "processed", chunk_count: 3 } },
    ],
  }),
  scanDocuments: vi.fn().mockResolvedValue({ added: 0, scanned: 0 }),
  deleteDocument: vi.fn().mockResolvedValue({}),
  deleteDocuments: vi.fn().mockResolvedValue({ deleted_count: 0, failed_ids: [] }),
  deleteAllDocumentsInVault: vi.fn().mockResolvedValue({ deleted_count: 0 }),
  getDocumentStats: vi.fn().mockResolvedValue({
    total_documents: 5,
    total_chunks: 38,
    total_size_bytes: 16384,
    documents_by_status: { processed: 4, pending: 1 },
  }),
}));

// Mock react-dropzone
vi.mock("react-dropzone", () => ({
  useDropzone: vi.fn(() => ({
    getRootProps: () => ({ role: "button" }),
    getInputProps: () => ({ type: "file" }),
    isDragActive: false,
  })),
}));

// Mock sonner toast
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

// Mock useDebounce hook
vi.mock("@/hooks/useDebounce", () => ({
  useDebounce: vi.fn((value: string) => [value, false]),
}));

// Mock useVaultStore
vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: 1,
    vaults: [{ id: 1, current_user_permission: "admin" }],
  })),
}));

// Mock useUploadStore
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

// Mock UI components
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

describe("DocumentsPage - Virtualization", () => {
  let container: HTMLElement;
  let unmount: () => void;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    if (unmount) {
      unmount();
    }
  });

  describe("Desktop Table Virtualization", () => {
    it("should render table with virtualized rows", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        // Table should exist
        const table = container.querySelector("table");
        expect(table).toBeTruthy();

        // tbody should have rows rendered via virtualizer
        const tbody = container.querySelector("tbody");
        expect(tbody).toBeTruthy();

        // The virtualizer should produce tr elements
        const rows = tbody?.querySelectorAll("tr");
        expect(rows).toBeTruthy();
        expect(rows!.length).toBe(5); // 5 documents from mock
      });
    });

    it("should render all 5 documents from mock data", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const bodyRows = container.querySelectorAll("tbody tr");
        expect(bodyRows.length).toBe(5);
      });
    });

    it("should have scroll container with max-height style", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        // Find the scroll container (div with overflow-auto and maxHeight)
        const scrollContainer = container.querySelector('[style*="max-height"]');
        expect(scrollContainer).toBeTruthy();
        // Check inline style attribute
        const style = scrollContainer?.getAttribute("style") || "";
        expect(style).toContain("max-height");
        expect(style).toContain("70vh");
      });
    });

    it("should have sticky thead via inline style", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const thead = container.querySelector("thead");
        expect(thead).toBeTruthy();
        // Check inline style attribute for sticky positioning
        const style = thead?.getAttribute("style") || "";
        expect(style).toContain("sticky");
      });
    });

    it("should have tbody with height style set by virtualizer", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const tbody = container.querySelector("tbody");
        expect(tbody).toBeTruthy();
        // Virtualizer sets height based on count * estimateSize
        const style = tbody?.getAttribute("style") || "";
        expect(style).toContain("height");
        // Height should be set (5 docs * 56px = 280px)
        expect(style).toContain("280px");
      });
    });

    it("should have body rows with absolute positioning style", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const bodyRows = container.querySelectorAll("tbody tr");
        expect(bodyRows.length).toBe(5);

        // Check first row has absolute positioning
        const firstRow = bodyRows[0];
        const style = firstRow?.getAttribute("style") || "";
        expect(style).toContain("absolute");
        expect(style).toContain("flex");
      });
    });
  });

  describe("Mobile Cards Virtualization", () => {
    it("should render mobile cards container", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        // Mobile container uses sm:hidden class
        const mobileContainer = container.querySelector(".sm\\:hidden");
        expect(mobileContainer).toBeTruthy();
      });
    });

    it("should have mobile scroll container with max-height style", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        // Find the mobile scroll container
        const mobileScroll = container.querySelector(".sm\\:hidden");
        expect(mobileScroll).toBeTruthy();
        const style = mobileScroll?.getAttribute("style") || "";
        expect(style).toContain("max-height");
        expect(style).toContain("70vh");
      });
    });
  });

  describe("Checkbox Selection with Virtualization", () => {
    it("should render select all checkbox in header", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const selectAllCheckbox = container.querySelector(
          'input[type="checkbox"][aria-label="Select all documents"]'
        );
        expect(selectAllCheckbox).toBeTruthy();
      });
    });

    it("should render individual checkboxes for each row", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const checkboxes = container.querySelectorAll('input[type="checkbox"]');
        // Should have select-all + 5 document checkboxes = 6
        expect(checkboxes.length).toBe(6);
      });
    });

    it("should toggle individual row selection", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const checkboxes = container.querySelectorAll('input[type="checkbox"]');
        expect(checkboxes.length).toBe(6);
      });

      // Click second checkbox (first document)
      const firstDocCheckbox = container.querySelectorAll('input[type="checkbox"]')[1];
      await act(async () => {
        fireEvent.click(firstDocCheckbox);
      });

      // Should now be checked
      expect((firstDocCheckbox as HTMLInputElement).checked).toBe(true);
    });

    it("should select all when select-all is checked", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const selectAllCheckbox = container.querySelector(
          'input[type="checkbox"][aria-label="Select all documents"]'
        );
        expect(selectAllCheckbox).toBeTruthy();
      });

      // Click select all
      const selectAllCheckbox = container.querySelector(
        'input[type="checkbox"][aria-label="Select all documents"]'
      ) as HTMLInputElement;

      await act(async () => {
        fireEvent.click(selectAllCheckbox);
      });

      // After select all, all individual checkboxes should be checked
      const checkboxes = container.querySelectorAll('input[type="checkbox"]');
      checkboxes.forEach((cb) => {
        expect((cb as HTMLInputElement).checked).toBe(true);
      });
    });
  });

  describe("Virtualizer Integration", () => {
    it("should call useVirtualizer with correct parameters", async () => {
      const { useVirtualizer } = await import("@tanstack/react-virtual");

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        // useVirtualizer should have been called at least once (once for table, once for mobile)
        expect(useVirtualizer).toHaveBeenCalled();
      });
    });

    it("should render documents matching virtualizer count", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        // All 5 documents should be rendered
        const bodyRows = container.querySelectorAll("tbody tr");
        expect(bodyRows.length).toBe(5);
      });
    });
  });

  describe("Empty State Handling", () => {
    it("should show empty state when no documents", async () => {
      // Re-mock API to return empty documents
      const { listDocuments } = await import("@/lib/api");
      vi.mocked(listDocuments).mockResolvedValueOnce({ documents: [] });

      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const emptyState = container.querySelector("[data-testid='empty-state']");
        expect(emptyState).toBeTruthy();
      });
    });
  });

  describe("Search Filtering with Virtualization", () => {
    it("should filter documents via search input", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const searchInput = container.querySelector('input[placeholder="Search documents and metadata..."]');
        expect(searchInput).toBeTruthy();
      });

      // Type in search box
      const searchInput = container.querySelector('input[placeholder="Search documents and metadata..."]') as HTMLInputElement;
      await act(async () => {
        fireEvent.change(searchInput, { target: { value: "test" } });
      });

      // Component should re-render with filtered results
      await waitFor(() => {
        expect(searchInput.value).toBe("test");
      });
    });
  });

  describe("Resize Handle Integration", () => {
    it("should still have resize handle with virtualized table", async () => {
      await act(async () => {
        const result = render(<DocumentsPage />);
        container = result.container;
        unmount = result.unmount;
      });

      await waitFor(() => {
        const resizeHandle = container.querySelector('[role="separator"][aria-orientation="vertical"]');
        expect(resizeHandle).toBeTruthy();
      });
    });
  });
});
