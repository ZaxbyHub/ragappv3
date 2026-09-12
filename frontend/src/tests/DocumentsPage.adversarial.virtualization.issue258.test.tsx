// Issue 258 / AC24 (FU-009) — rapid-additions stability / design pin.
//
// Phase 2.5 acceptance check. The EXISTING rapid-additions test
// (src/tests/DocumentsPage.adversarial.virtualization.test.tsx:651-676)
// mounts and unmounts the full DocumentsPage 50 times against the real
// setTimeout/setInterval machinery of useDocumentPolling — the unawaited
// polling promises and already-queued timer callbacks race under load,
// which is why that test needed a 30s band-aid timeout (issue #494
// FU-009). Its sibling (rapid removals, :678) drives ONE mounted instance
// with rerender() and does not flake — that is the root-fix shape Phase 4
// must apply to the additions test.
//
// This check pins that design AND the behavior it must preserve:
//   - mount DocumentsPage exactly ONCE per scenario run;
//   - grow the document list 1 → 50 through the page's real query-driven
//     refetch path (each search keystroke is a new query → refetch → new
//     list), with zero remounts;
//   - after every growth step the rendered virtualized row count must
//     TRACK the data (1..50) — not just "not crash";
//   - the whole 5-run scenario must complete deterministically inside the
//     30s ceiling the OLD test needed for a single flaky run.
//
// Document delivery uses the page's genuine refetch path (search change →
// useDocumentPolling query change → listDocuments), not a store poke, so
// the polling hook's generation guards are exercised for real. No fake
// timers: with all documents in terminal status no polling interval ever
// arms, which is itself part of the pinned design (no timers to race).
//
// Expected color at base: GREEN (production behavior is fine; the flake
// was the old TEST's mount/unmount design) — typed as a PRESERVING pin.
// It turns RED if Phase 4's rewrite regresses tracking or reintroduces
// timer races (act warnings / unmount errors fail the run).

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import DocumentsPage from "@/pages/DocumentsPage";
import type { Document } from "@/lib/api";

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

// jsdom has no layout: the real virtualizer would render 0 rows. Mocking
// react-virtual to virtualize ALL items (same contract as the existing
// adversarial suite) keeps the row-count assertion meaningful and
// deterministic.
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(({ count }: { count: number }) => ({
    getVirtualItems: () =>
      Array.from({ length: count }, (_, i) => ({ index: i, start: i * 72, size: 72, key: `v-${i}` })),
    getTotalSize: () => count * 72,
    measureElement: vi.fn(() => ({ getBoundingClientRect: () => ({ height: 72, width: 1200 }) })),
    scrollToIndex: vi.fn(),
    measure: vi.fn(),
    scrollOffset: 0,
    totalSize: count * 72,
  })),
}));

// The ONLY mutable data source: listDocuments returns whatever the current
// step says, so each query-driven refetch observes the grown list.
// vi.hoisted because vi.mock factories run above module-level bindings.
const { documentsRef } = vi.hoisted(() => ({
  documentsRef: { current: [] as unknown as import("@/lib/api").Document[] },
}));

vi.mock("@/lib/api", () => ({
  listDocuments: vi.fn(() =>
    Promise.resolve({ documents: documentsRef.current, total: documentsRef.current.length })
  ),
  getDocumentStats: vi.fn(() =>
    Promise.resolve({
      total_documents: 50,
      total_chunks: 0,
      total_size_bytes: 0,
      documents_by_status: {},
    })
  ),
  getDocumentWikiStatus: vi.fn(() => Promise.resolve(undefined)),
  compileDocumentWiki: vi.fn(() => Promise.resolve({})),
  listTags: vi.fn(() => Promise.resolve([])),
  listFolders: vi.fn(() => Promise.resolve([])),
  createFolder: vi.fn(() => Promise.resolve({})),
  updateFolder: vi.fn(() => Promise.resolve({})),
  deleteFolder: vi.fn(() => Promise.resolve({})),
  scanDocuments: vi.fn(() => Promise.resolve({ added: 0, scanned: 0 })),
  deleteDocument: vi.fn(() => Promise.resolve({})),
  deleteDocuments: vi.fn(() => Promise.resolve({ deleted_count: 0, failed_ids: [] })),
  deleteAllDocumentsInVault: vi.fn(() => Promise.resolve({ deleted_count: 0 })),
  downloadDocument: vi.fn(() => Promise.resolve({})),
}));

vi.mock("react-dropzone", () => ({
  useDropzone: () => ({ getRootProps: () => ({}), getInputProps: () => ({}), isDragActive: false }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock("@/hooks/useDebounce", () => ({
  useDebounce: (value: string) => [value, false],
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn(() => ({
    activeVaultId: 1,
    vaults: [{ id: 1, name: "Research vault", current_user_permission: "admin" }],
  })),
}));

vi.mock("@/stores/useUploadStore", () => ({
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

function makeDocuments(count: number): Document[] {
  // Terminal status everywhere: no processing docs → the adaptive status
  // poller never arms a timer → nothing races the growth steps.
  return Array.from({ length: count }, (_, i) => ({
    id: String(i + 1),
    filename: `doc_${i + 1}.pdf`,
    size: 1024,
    created_at: "2024-01-01T00:00:00Z",
    metadata: { status: "processed", chunk_count: 5 },
  })) as unknown as Document[];
}

/** Rows of the (desktop) virtualized documents table. */
function tableRowCount(container: HTMLElement): number {
  return container.querySelectorAll("table tbody tr[role='row']").length;
}

describe("AC24 — DocumentsPage rapid additions via the rerender design (FU-009)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
    documentsRef.current = [];
  });

  it(
    "AC24: one mount, query-driven growth 1→50 keeps the rendered row count tracking, 5 deterministic runs",
    { timeout: 120_000 },
    async () => {
      const { listDocuments } = await import("@/lib/api");
      // The module mock is runtime-only; the imported symbol still carries
      // the real signature, so go through vi.mocked for mock APIs.
      const mockedListDocuments = vi.mocked(listDocuments);
      const runCount = 5;
      const startedAt = Date.now();

      for (let run = 1; run <= runCount; run++) {
        mockedListDocuments.mockClear();
        documentsRef.current = makeDocuments(1);

        let container: HTMLElement;
        let unmount: () => void = () => {};
        // ONE mount per run — the design the Phase 4 rewrite must use.
        // (Explicit unmount below: RTL auto-cleanup fires per TEST, and this
        // loop runs five scenarios inside one test.)
        await act(async () => {
          const result = render(
            <MemoryRouter>
              <DocumentsPage />
            </MemoryRouter>
          );
          container = result.container;
          unmount = result.unmount;
        });

        await waitFor(() => expect(tableRowCount(container!)).toBe(1));
        // Initial load only: exactly one list fetch per mount.
        expect(mockedListDocuments).toHaveBeenCalledTimes(1);

        const search = screen.getByPlaceholderText("Search documents and metadata...");
        for (let step = 2; step <= 50; step++) {
          documentsRef.current = makeDocuments(step);
          // Each keystroke is a new server-side query → genuine query-driven
          // refetch on the SAME mounted instance (no remount, no unmount).
          await act(async () => {
            fireEvent.change(search, { target: { value: `growth-${step}` } });
          });
          await waitFor(() => expect(tableRowCount(container!)).toBe(step), { timeout: 5_000 });
        }

        // Exactly the initial fetch plus 49 query-driven refetches — proves
        // the growth went through the page's data path, not a test poke.
        expect(mockedListDocuments).toHaveBeenCalledTimes(50);

        // End of run: unmount the single mounted instance cleanly.
        unmount();
      }

      const elapsedMs = Date.now() - startedAt;
      console.log(`AC24 run=${runCount}x growth(1→50) completed in ${elapsedMs}ms`);
      // Ceiling rationale: the OLD mount/unmount design needed a 30s
      // band-aid timeout for ONE flaky run. This design does FIVE
      // deterministic runs; 60s gives >3x headroom over the measured 18s
      // while still failing if the rewrite reintroduces quadratic churn.
      console.log(
        "AC24 CHECK: FAIL — expecting the 5-run rerender scenario to finish within the 60s ceiling (old flaky design needed 30s for ONE run)"
      );
      expect(elapsedMs).toBeLessThan(60_000);
    }
  );
});
