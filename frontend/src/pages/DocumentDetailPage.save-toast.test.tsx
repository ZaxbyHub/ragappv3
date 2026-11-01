// Issue #764 AC6 — toast-side pins for the late-tag-save fix (non-frozen
// companion to frozen check C6, which asserts only the Save-button state).
//
// Two halves, both NEW-behavior pins written with the fix (not acceptance
// criteria — the issue's ACs never required the toast, but the fix changes
// toast behavior and unpinned behavior drifts):
//   (i)  happy path: saving tags on the CURRENT document still toasts
//        "Tags updated" and Save ends disabled (tagsDirty false);
//   (ii) suppression: a save that resolves AFTER navigating to another
//        document must NOT toast — the commit guard already drops the state
//        write; the toast read must agree (docRef live compare, not the
//        closed-over doc, which would be a tautology).

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { toast } from "sonner";

import DocumentDetailPage from "@/pages/DocumentDetailPage";
import * as api from "@/lib/api";
import type { Document, Tag } from "@/lib/api";

vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    dismiss: vi.fn(),
  }),
}));

vi.mock("@/hooks/useUploadMonitoring", () => ({
  useUploadMonitoring: () => ({}),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getDocument: vi.fn(),
    listTags: vi.fn(),
    setDocumentTags: vi.fn(),
    getDocumentRawBlob: vi.fn(() => Promise.reject(new Error("no preview"))),
  };
});

// #767 AC4 companion: the detail page now gates Save on the document's vault
// permission; these suites exercise the WRITE path, so the vault store mock
// reports write for the documents' vaults (permission-aware since this PR).
const { vaultState } = vi.hoisted(() => ({
  vaultState: {
    vaults: [
      { id: 1, name: "Vault One", current_user_permission: "write" },
      { id: 2, name: "Vault Two", current_user_permission: "write" },
    ],
    activeVaultId: 1,
  },
}));
vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: vi.fn(
    (selector?: (state: typeof vaultState) => unknown) =>
      selector ? selector(vaultState) : vaultState
  ),
}));

const doc1 = {
  id: 1,
  filename: "one.txt",
  size: 10,
  vault_id: 1,
  created_at: "2024-01-01T00:00:00Z",
  metadata: { status: "indexed" },
  tags: [{ id: 5, name: "alpha" }],
} as unknown as Document;

const doc2 = {
  id: 2,
  filename: "two.txt",
  size: 10,
  vault_id: 1,
  created_at: "2024-01-02T00:00:00Z",
  metadata: { status: "indexed" },
  tags: [],
} as unknown as Document;

const tagList = [
  { id: 5, name: "alpha" },
  { id: 6, name: "beta" },
] as unknown as Tag[];

function makeRouter() {
  return createMemoryRouter(
    [
      { path: "/documents/:documentId", element: <DocumentDetailPage /> },
    ],
    { initialEntries: ["/documents/1"] }
  );
}

describe("AC6 toast pins (issue #764, non-frozen)", () => {
  beforeEach(() => {
    vi.mocked(toast.success).mockReset();
    vi.mocked(toast.error).mockReset();
    vi.mocked(api.getDocument).mockImplementation(async (id: number) => {
      if (id === 1) return doc1;
      if (id === 2) return doc2;
      throw new Error(`unexpected id ${id}`);
    });
    vi.mocked(api.listTags).mockResolvedValue(tagList);
    vi.mocked(api.setDocumentTags).mockImplementation(
      // Echo what was sent (a real server persists the posted ids): the
      // happy path toggles beta ON, so the saved list is alpha+beta and
      // tagsDirty resolves false after commit.
      async (_id: number, _vaultId: number, tagIds: number[]) =>
        tagIds.map((tid) =>
          tid === 6
            ? ({ id: 6, name: "beta" } as Tag)
            : ({ id: 5, name: "alpha" } as Tag)
        )
    );
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("happy-path save on the current document toasts success and ends disabled", { timeout: 30_000 }, async () => {
    const router = makeRouter();
    render(<RouterProvider router={router} />);

    // Document 1 with tag alpha pre-selected; toggle beta on to make tagsDirty.
    const beta = await screen.findByLabelText("Tag beta", { timeout: 10_000 });
    await act(async () => {
      beta.click();
    });
    const save = await screen.findByRole("button", { name: /save/i }, { timeout: 10_000 });
    await waitFor(() => expect(save).toBeEnabled(), { timeout: 10_000 });

    await act(async () => {
      save.click();
    });

    await waitFor(
      () => expect(vi.mocked(toast.success)).toHaveBeenCalledWith("Tags updated"),
      { timeout: 10_000 }
    );
    await waitFor(() => expect(save).toBeDisabled(), { timeout: 10_000 });
  });

  it("stale save resolving after navigation does not toast success", { timeout: 30_000 }, async () => {
    const setDocumentTags = vi.mocked(api.setDocumentTags);
    let releaseSave!: (tags: Tag[]) => void;
    setDocumentTags.mockImplementationOnce(
      () =>
        new Promise<Tag[]>((resolve) => {
          releaseSave = resolve;
        })
    );

    const router = makeRouter();
    render(<RouterProvider router={router} />);

    const alpha = await screen.findByLabelText("Tag alpha");
    await act(async () => {
      alpha.click();
    });
    const save = await screen.findByRole("button", { name: /save/i }, { timeout: 10_000 });
    await waitFor(() => expect(save).toBeEnabled(), { timeout: 10_000 });

    // Start the save (held pending), then navigate to doc 2.
    await act(async () => {
      save.click();
    });
    await act(async () => {
      await router.navigate("/documents/2");
    });
    // Doc 2 must be on screen before the save resolves.
    await screen.findByRole("heading", { name: /two\.txt/i }, { timeout: 10_000 });

    // Resolve the stale save — no toast, no tag on doc 2.
    await act(async () => {
      releaseSave([{ id: 5, name: "alpha" }] as Tag[]);
    });

    expect(vi.mocked(toast.success)).not.toHaveBeenCalledWith(
      "Tags updated"
    );
    // Doc 2's Save stays disabled: tagsDirty never became true from the
    // stale commit (the frozen C6 covers this state assertion; kept here as
    // a belt-and-suspenders pairing with the toast assertion).
    const saveAfter = screen.getByRole("button", { name: /save/i });
    await waitFor(() => expect(saveAfter).toBeDisabled(), { timeout: 10_000 });
  });
});
