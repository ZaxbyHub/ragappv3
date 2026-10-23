// frontend/src/pages/DocumentDetailPage.k01.test.tsx
// k01 acceptance checks — DocumentDetailPage tag state across in-app
// navigation between documents.
//
// Two arm's-length checks, both expected RED at base:
//   AC6 — a tag save started on document 1 whose response arrives AFTER the
//         user navigated to document 2 must not commit document 1's tag list
//         onto document 2. At base handleSaveTags commits setDoc(...) with the
//         (stale) server response unconditionally, so doc 2 inherits the tags,
//         tagsDirty flips true, and the Save button re-enables.
//   AC7 — a FAILED listTags for the next document's vault must still hide the
//         previous vault's tag checkboxes. At base load()'s catch ignores
//         listTags failures and `allTags` is never reset, so the previous
//         vault's checkboxes keep rendering.
//
// Mock structure mirrors DocumentDetailPage.preview-race.test.tsx (hoisted
// apiMock, sonner, in-router <Link> navigation, deferred promises), plus the
// native-input checkbox render-through mock from DocumentsPage.test.tsx so
// tag checkboxes can be ticked and queried by their aria-label.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, act, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { MemoryRouter, Routes, Route, Link } from "react-router-dom";

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

vi.mock("@/hooks/useUploadMonitoring", () => ({
  useUploadMonitoring: () => {},
}));

// Native-input render-through checkbox: aria-label flows onto the <input>, so
// tag checkboxes are addressable via getByLabelText("Tag <name>") and toggled
// with fireEvent.click (repo-proven pattern from DocumentsPage.test.tsx).
vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({ onCheckedChange, checked, ...props }: { onCheckedChange?: (checked: boolean) => void; checked?: boolean }) => (
    <input type="checkbox" onChange={(e) => onCheckedChange?.(e.target.checked)} checked={checked} {...props} />
  ),
}));

import DocumentDetailPage from "@/pages/DocumentDetailPage";

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const fakeBlob = (text: string) => ({ text: async () => text });

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

const TAG_ALPHA = { id: 5, name: "alpha", document_count: 0 };
const TAG_BETA = { id: 6, name: "beta", document_count: 0 };
const TAG_V1_ONLY = { id: 9, name: "v1-only", document_count: 0 };

// AC6 fixtures — both documents live in vault 1; doc 1 has "alpha", doc 2 is
// untagged.
const DOC1 = {
  id: "1",
  filename: "one.txt",
  vault_id: 1,
  size: 10,
  created_at: "2024-01-01",
  metadata: { status: "indexed", chunk_count: 1 },
  tags: [TAG_ALPHA],
};
const DOC2_SAME_VAULT = {
  id: "2",
  filename: "two.txt",
  vault_id: 1,
  size: 20,
  created_at: "2024-01-02",
  metadata: { status: "indexed", chunk_count: 2 },
  tags: [],
};

// AC7 fixtures — doc 1 in vault 1, doc 2 in vault 2; both untagged (the tag
// list under test is the VAULT tag list rendered as checkboxes).
const DOC1_VAULT1 = {
  id: "1",
  filename: "one.txt",
  vault_id: 1,
  size: 10,
  created_at: "2024-01-01",
  metadata: { status: "indexed", chunk_count: 1 },
  tags: [],
};
const DOC2_VAULT2 = {
  id: "2",
  filename: "two.txt",
  vault_id: 2,
  size: 20,
  created_at: "2024-01-02",
  metadata: { status: "indexed", chunk_count: 2 },
  tags: [],
};

// In-router navigation harness (same pattern as the preview-race suite): the
// route change re-renders the SAME DocumentDetailPage instance, so page state
// (allTags, selectedTagIds, savingTags) persists across the navigation.
function Harness() {
  return (
    <MemoryRouter initialEntries={["/documents/1"]}>
      <Link to="/documents/2" data-testid="nav-to-2">
        go to 2
      </Link>
      <Routes>
        <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.listTags.mockResolvedValue([TAG_ALPHA, TAG_BETA]);
  apiMock.setDocumentTags.mockResolvedValue([]);
  apiMock.downloadDocument.mockResolvedValue(undefined);
  apiMock.getDocumentRawBlob.mockImplementation(() => Promise.resolve(fakeBlob("PREVIEW-CONTENT")));
});

describe("DocumentDetailPage k01 — stale tag state across navigation", () => {
  it("late tag save for the previous document does not touch the current document", { timeout: 30_000 }, async () => {
    apiMock.getDocument.mockImplementation((id: unknown) =>
      Number(id) === 1 ? Promise.resolve(DOC1) : Promise.resolve(DOC2_SAME_VAULT)
    );
    // The save response is held pending so it can land AFTER the navigation.
    const saveGate = deferred<unknown>();
    apiMock.setDocumentTags.mockImplementation(() => saveGate.promise);

    await act(async () => {
      render(<Harness />);
    });

    // Doc 1 loaded with the vault tag list; "alpha" starts checked.
    const alphaCheckbox = (await screen.findByLabelText("Tag alpha", {}, { timeout: 10_000 })) as HTMLInputElement;
    expect(alphaCheckbox.checked).toBe(true);

    // Uncheck "alpha" — selection diverges from the document, Save enables.
    await act(async () => {
      fireEvent.click(alphaCheckbox);
    });
    const saveButton = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    await waitFor(() => expect(saveButton).not.toBeDisabled(), { timeout: 5_000 });

    // Save document 1's tag change — the request is held pending.
    await act(async () => {
      fireEvent.click(saveButton);
    });
    expect(apiMock.setDocumentTags).toHaveBeenCalledWith(1, 1, []);

    // Navigate to document 2 while the save is in flight.
    await act(async () => {
      fireEvent.click(screen.getByTestId("nav-to-2"));
    });
    await waitFor(
      () => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("two.txt"),
      { timeout: 5_000 }
    );
    // Doc 2's load fully settled (its getDocument AND its listTags landed).
    await waitFor(() => expect(apiMock.listTags.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 5_000 });
    await act(async () => {
      await flushMicrotasks();
    });
    // Doc 2 is untagged and nothing is dirty: Save must be disabled here.
    const saveOnDoc2 = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(saveOnDoc2.disabled).toBe(true);

    // The held save response (document 1's tag list) resolves LATE — after
    // document 2 is the document on screen.
    console.log(
      "K01/AC6 CHECK: FAIL — expecting Save to remain disabled when a tag save for the previous document resolves late"
    );
    await act(async () => {
      saveGate.resolve([TAG_ALPHA]);
      await flushMicrotasks();
    });

    // At base handleSaveTags commits setDoc({...prev, tags: [alpha]}) onto
    // document 2 → tagsDirty true → Save re-enables → RED.
    const saveAfterLateResolution = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(saveAfterLateResolution.disabled).toBe(true);
  });

  it("failed tag list load for the next document hides the previous vault tags", { timeout: 30_000 }, async () => {
    apiMock.getDocument.mockImplementation((id: unknown) =>
      Number(id) === 1 ? Promise.resolve(DOC1_VAULT1) : Promise.resolve(DOC2_VAULT2)
    );
    apiMock.listTags.mockImplementation((vaultId: number) =>
      vaultId === 1
        ? Promise.resolve([TAG_V1_ONLY])
        : Promise.reject(new Error("vault 2 tags unavailable"))
    );

    await act(async () => {
      render(<Harness />);
    });

    // Vault 1's tag list rendered as a checkbox.
    await screen.findByLabelText("Tag v1-only", {}, { timeout: 10_000 });

    // Navigate to document 2 (vault 2), whose listTags call FAILS.
    await act(async () => {
      fireEvent.click(screen.getByTestId("nav-to-2"));
    });
    await waitFor(
      () => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("two.txt"),
      { timeout: 5_000 }
    );
    await waitFor(() => expect(apiMock.listTags).toHaveBeenCalledWith(2), { timeout: 5_000 });
    // Let the rejection propagate through load()'s await/catch.
    await act(async () => {
      await flushMicrotasks();
    });

    // At base the catch ignores listTags failures and allTags is never reset —
    // the vault-1 checkbox keeps rendering → RED.
    console.log(
      "K01/AC7 CHECK: FAIL — expecting the vault-1 tag checkbox to disappear after a failed tag list load for vault 2"
    );
    expect(screen.queryAllByLabelText("Tag v1-only").length).toBe(0);
  });
});
