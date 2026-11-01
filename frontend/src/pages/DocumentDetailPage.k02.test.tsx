// frontend/src/pages/DocumentDetailPage.k02.test.tsx
// k02 feedback-round pins: loading-window toast suppression (PRR-001) and AC6
// state-corruption direction (PR #1012 swarm-pr-feedback round).
//
// Two pins over the uncommitted working-tree fix, both expected GREEN:
//   (i)  PRR-001 — a tag save that settles while the NEXT document's load is
//        still in flight (the page sits on "Loading…") must NOT toast success.
//        The loadGen half of the still-current guard is what closes this
//        window: during loading docRef still names the PREVIOUS document, so
//        an id-only guard would let the stale save toast over the loading
//        screen (the old code did exactly that).
//   (ii) AC6 state-corruption direction — the same late save must not touch
//        the current document's tag checkboxes or its tagsDirty bookkeeping:
//        doc 2's "Tag alpha" stays unchecked and Save stays disabled. The
//        OUTCOME is pinned jointly by the loadGen gate and this assertion:
//        removing only the updater's prev-id check does NOT fail this test
//        (the outer gate returns before setDoc in every late-save scenario —
//        probe R2-P5), so that check is a redundant backstop; what these
//        pins kill is removal of the outer gate itself (probe R2-P2).
//
// Harness mirrors DocumentDetailPage.save-toast.test.tsx (proven in this
// repo): deferred getDocument/setDocumentTags mocks, the useUploadMonitoring
// stub, and createMemoryRouter navigation.

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
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

// Both alpha and beta are in the vault vocabulary so doc 2's checkboxes
// render (doc 2 ships with no tags → both start unchecked).
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

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i += 1) await Promise.resolve();
}

describe("DocumentDetailPage k02 — feedback-round pins (issue #764 / PR #1012)", () => {
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
      // Echo what was sent (a real server persists the posted ids).
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

  it("tag save settling during the next document's load does not toast success", { timeout: 30_000 }, async () => {
    // Doc 2's getDocument is HELD: navigating parks the page on "Loading…".
    let releaseDoc2!: (document: Document) => void;
    const heldDoc2 = new Promise<Document>((resolve) => {
      releaseDoc2 = resolve;
    });
    vi.mocked(api.getDocument).mockImplementation(async (id: number) => {
      if (id === 1) return doc1;
      if (id === 2) return heldDoc2;
      throw new Error(`unexpected id ${id}`);
    });

    // The doc-1 save is HELD so its settlement lands inside the loading window.
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

    // Doc 1 with tag alpha pre-selected; toggle beta on to make tagsDirty.
    const beta = await screen.findByLabelText("Tag beta");
    await act(async () => {
      beta.click();
    });
    const save = await screen.findByRole("button", { name: /save/i });
    await waitFor(() => expect(save).toBeEnabled());

    // Start the save (held pending), then navigate to doc 2.
    await act(async () => {
      save.click();
    });
    await act(async () => {
      await router.navigate("/documents/2");
    });
    // Doc 2's load is still in flight: the page must sit on the loading state.
    await screen.findByText("Loading…");

    // The stale doc-1 save settles INSIDE the loading window — docRef still
    // names doc 1 here, so only the loadGen half of the guard suppresses the
    // toast. The old code toasted "Tags updated" over this screen.
    await act(async () => {
      releaseSave([{ id: 5, name: "alpha" }] as Tag[]);
      await flushMicrotasks();
    });

    expect(vi.mocked(toast.success)).not.toHaveBeenCalledWith("Tags updated");

    // Settle doc 2's held load so no flight outlives the test.
    await act(async () => {
      releaseDoc2(doc2);
      await flushMicrotasks();
    });
  });

  it("late tag save leaves the current document's tag checkboxes unchanged", { timeout: 30_000 }, async () => {
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

    // Doc 1 ships with alpha selected; toggle alpha OFF to make tagsDirty.
    const alpha = await screen.findByLabelText("Tag alpha");
    await act(async () => {
      alpha.click();
    });
    const save = await screen.findByRole("button", { name: /save/i });
    await waitFor(() => expect(save).toBeEnabled());

    // Start the save (held pending), then navigate to doc 2 — its load runs
    // to completion (heading + tag checkboxes on screen) BEFORE the save
    // settles, so the stale resolution meets a fully-loaded doc 2.
    await act(async () => {
      save.click();
    });
    await act(async () => {
      await router.navigate("/documents/2");
    });
    await screen.findByRole("heading", { name: /two\.txt/i });
    const alphaOnDoc2 = await screen.findByLabelText("Tag alpha");

    // The stale doc-1 save settles now — nothing on doc 2 may move.
    await act(async () => {
      releaseSave([{ id: 5, name: "alpha" }] as Tag[]);
      await flushMicrotasks();
    });

    // Doc 2 has no tags: alpha stays unchecked. With the setDoc updater guard
    // removed, the stale tags land on doc 2, tagsDirty flips true, and Save
    // re-enables — the second assertion kills that mutant.
    expect(alphaOnDoc2).not.toBeChecked();
    const saveAfter = screen.getByRole("button", { name: /save/i });
    expect(saveAfter).toBeDisabled();
  });
});
