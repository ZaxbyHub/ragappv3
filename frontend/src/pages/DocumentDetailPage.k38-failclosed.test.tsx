/**
 * DocumentDetailPage K-38 fail-closed pin (PR #1048 feedback round).
 *
 * A document whose vault permission is UNRESOLVED (vault absent from the
 * store, or current_user_permission undefined) must NOT offer an enabled tag
 * Save: the gate added for issue #767 AC4 must fail closed, not treat
 * "unknown" as writable. Cloned from the frozen DocumentDetailPage.k04.test
 * harness (non-frozen sibling; the frozen file stays byte-locked).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act, screen, fireEvent, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter, Routes, Route } from "react-router-dom";

const apiMock = vi.hoisted(() => ({
  getDocument: vi.fn(),
  getDocumentRawBlob: vi.fn(),
  setDocumentTags: vi.fn(),
  downloadDocument: vi.fn(),
  listTags: vi.fn(),
}));

vi.mock("@/lib/api", () => apiMock);

vi.mock("sonner", () => {
  const toast = Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
    dismiss: vi.fn(),
  });
  return { toast };
});

// The vault store mock: the document's vault reports read-only access. At
// base the page never reads it; the permission gate added by the fix does.
const { vaultState } = vi.hoisted(() => ({
  vaultState: {
    vaults: [] as Array<{
      id: number;
      name: string;
      current_user_permission: string;
    }>,
    activeVaultId: null as number | null,
  },
}));
vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn(
    (selector?: (state: typeof vaultState) => unknown) =>
      selector ? selector(vaultState) : vaultState
  ),
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, onClick, disabled, ...props }: {
    children: React.ReactNode;
    onClick?: () => void;
    disabled?: boolean;
  }) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}));

vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({ onCheckedChange, checked, ...props }: {
    onCheckedChange?: (checked: boolean) => void;
    checked?: boolean;
  }) => (
    <input
      type="checkbox"
      onChange={(e) => onCheckedChange?.(e.target.checked)}
      checked={checked}
      {...props}
    />
  ),
}));

vi.mock("@/components/ui/card", () => ({
  Card: ({ children }: { children: React.ReactNode }) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  CardHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h3>{children}</h3>,
}));

vi.mock("@/components/ui/badge", () => ({
  Badge: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

import DocumentDetailPage from "@/pages/DocumentDetailPage";
import { useUploadStore } from "@/stores/useUploadStore";

const TAG_ALPHA = {
  id: 11,
  vault_id: 7,
  name: "alpha",
  color: "#000000",
  created_at: "2024-01-01",
  updated_at: "2024-01-01",
  document_count: 1,
};

// A non-previewable extension keeps the preview effect out of the picture.
const DOC = {
  id: "1",
  filename: "notes.docx",
  vault_id: 7,
  size: 10,
  created_at: "2024-01-01",
  metadata: { status: "indexed", chunk_count: 2 },
  tags: [TAG_ALPHA],
};

function Harness() {
  return (
    <MemoryRouter initialEntries={["/documents/1"]}>
      <Routes>
        <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
      </Routes>
    </MemoryRouter>
  );
}

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

describe("DocumentDetailPage K38 fail-closed Save pin (PR #1048 feedback)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vaultState.vaults = [];
    vaultState.activeVaultId = null;
    useUploadStore.setState({ uploads: [] });
    apiMock.getDocument.mockResolvedValue(DOC);
    apiMock.listTags.mockResolvedValue([TAG_ALPHA]);
    apiMock.setDocumentTags.mockResolvedValue([]);
    apiMock.downloadDocument.mockResolvedValue(undefined);
    apiMock.getDocumentRawBlob.mockResolvedValue({ text: async () => "" });
  });

  afterEach(async () => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    await act(async () => {
      await flushMicrotasks();
    });
  });

  it("unresolved vault permission keeps tag Save disabled (fail closed)", { timeout: 30_000 }, async () => {
    // The vault list is EMPTY: the document's vault is absent from the
    // store, so docVaultPermission resolves to null — Save must stay
    // disabled (no "unknown means writable" fallback).
    vaultState.vaults = [];
    vaultState.activeVaultId = null;

    await act(async () => {
      render(<Harness />);
      await flushMicrotasks();
    });

    // Sentinel: the document loaded and the tag vocabulary rendered.
    await waitFor(() => {
      expect(screen.getByText("Details")).toBeInTheDocument();
    });

    // Make the tag selection dirty (the server would reject the save with
    // 403 for this read-only viewer). If the checkbox is already gated for
    // a read-only viewer, there is nothing to dirty and the Save check below
    // must still hold. Hiding or disabling the editor both pass.
    const tagCheckbox = document.querySelector(
      'input[aria-label="Tag alpha"]'
    ) as HTMLInputElement | null;
    if (tagCheckbox && !tagCheckbox.disabled) {
      await act(async () => {
        fireEvent.click(tagCheckbox);
      });
    }

    const saveButtons = screen.queryAllByRole("button", { name: "Save" });
    const saveEnabled = saveButtons.some(
      (button) => !(button as HTMLButtonElement).disabled
    );
    expect(saveEnabled).toBe(false);

    // K-26: the disabled-state wording must be honest about WHY — unresolved
    // permission is not "write access required".
    const saveButtonsWithTitle = screen.queryAllByTitle(
      "Vault access could not be determined; save is unavailable"
    );
    expect(saveButtonsWithTitle.length).toBeGreaterThan(0);
  });
});
