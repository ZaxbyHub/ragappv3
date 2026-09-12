// Issue 258 / AC21 part (b) (ENH-005) — expanded a11y smoke surfaces.
//
// Phase 2.5 acceptance check. The existing a11y suite
// (src/a11y.smoke.test.tsx) covers 8 draft-room surfaces only. AC21
// expands coverage beyond draft-room: this file smoke-checks, with real
// axe, the two surfaces named by the issue trace —
//   1. DocumentsPage in its loading state (DocumentsTableSkeleton), and
//   2. LoginPage in its signed-out render (the first screen a user sees).
//
// The checks assert ZERO axe violations with no weakening (no disabled
// rules). If a surface has violations at base, that is a REAL finding to
// report and fix in Phase 4 — the assertion stays as-is.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import { axe } from "jest-axe";
import { MemoryRouter } from "react-router-dom";

import DocumentsPage from "@/pages/DocumentsPage";
import LoginPage from "@/pages/LoginPage";

// jsdom has no ResizeObserver; Radix Checkbox (DocumentsTableSkeleton)
// needs it to mount at all.
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

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

// Never-resolving fetches keep useDocumentPolling in its genuine loading
// state (the skeleton path), without fake timers. Inlined in each factory
// (vi.mock factories are hoisted above module-level bindings).
vi.mock("@/lib/api", () => ({
  listDocuments: vi.fn(() => new Promise<never>(() => {})),
  getDocumentStats: vi.fn(() => new Promise<never>(() => {})),
  getDocumentWikiStatus: vi.fn(() => new Promise<never>(() => {})),
  compileDocumentWiki: vi.fn(() => new Promise<never>(() => {})),
  listTags: vi.fn().mockResolvedValue([]),
  listFolders: vi.fn().mockResolvedValue([]),
  createFolder: vi.fn(),
  updateFolder: vi.fn(),
  deleteFolder: vi.fn(),
  scanDocuments: vi.fn(),
  deleteDocument: vi.fn(),
  deleteDocuments: vi.fn(),
  deleteAllDocumentsInVault: vi.fn(),
  downloadDocument: vi.fn(),
}));

vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: Object.assign(
    vi.fn(() => ({
      login: vi.fn(),
      needsSetup: false,
      isLoading: false,
      authMode: "jwt",
    })),
    { getState: () => ({ init: vi.fn().mockResolvedValue(undefined) }) }
  ),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

function summarizeViolations(violations: Awaited<ReturnType<typeof axe>>["violations"]) {
  return violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    nodes: v.nodes.map((n) => ({ target: n.target, html: n.html })),
  }));
}

describe("AC21 — a11y smoke surfaces beyond draft-room (ENH-005)", () => {
  it("AC21: DocumentsPage loading state has no axe violations", async () => {
    const { container } = render(
      <MemoryRouter>
        <DocumentsPage />
      </MemoryRouter>
    );

    const results = await axe(container);

    console.log(
      "AC21 CHECK: FAIL — DocumentsPage loading state axe violations:",
      JSON.stringify(summarizeViolations(results.violations), null, 2)
    );
    expect(results.violations).toHaveLength(0);
  }, 30_000);

  it("AC21: LoginPage signed-out render has no axe violations", async () => {
    const { container } = render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>
    );

    const results = await axe(container);

    console.log(
      "AC21 CHECK: FAIL — LoginPage render axe violations:",
      JSON.stringify(summarizeViolations(results.violations), null, 2)
    );
    expect(results.violations).toHaveLength(0);
  }, 30_000);
});
