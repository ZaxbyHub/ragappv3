/**
 * DocumentDetailPage preview race — REVERSED ordering vs frozen check C4
 * (issue #514 / UI-011 fixup from the Phase 4.6 critic).
 *
 * The frozen check (.agents/issue-traces/514-c3-upload-library-unify/repro/
 * C4-late-detail-response.sh) covers the ordering where the stale document's
 * getDocument resolves while the CURRENT document's preview blob is still
 * pending. This suite covers the critic's probe with the order REVERSED:
 * doc2's getDocument resolves, doc2's preview blob ("2") resolves and
 * COMMITS (TWO-CONTENT is visible), and only THEN does the stale doc1
 * getDocument response arrive.
 *
 * The load-generation guard must keep the stale resolution from calling
 * setPreviewSource: re-triggering the preview effect for the abandoned
 * document would run the previous effect's cleanup — nulling previewText and
 * erasing the preview the user is reading — and restart a blob fetch for a
 * document nobody is looking at. Before the fix, the committed TWO-CONTENT
 * preview was torn down by that cleanup even though the stale blob itself
 * could never commit.
 *
 * Mock/wiring style mirrors the frozen C4 spec (same getDocument /
 * getDocumentRawBlob / listTags fakes, same in-router Link navigation).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, act, screen } from "@testing-library/react";
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

const ALL_TAGS = [
  { id: 10, name: "AlphaTag", document_count: 0 },
  { id: 20, name: "BetaTag", document_count: 0 },
];

const DOC1 = {
  id: "1",
  filename: "one.txt",
  vault_id: 1,
  size: 10,
  created_at: "2024-01-01",
  metadata: { status: "indexed", chunk_count: 1 },
  tags: [{ id: 20, name: "BetaTag", document_count: 0 }],
};
const DOC2 = {
  id: "2",
  filename: "two.txt",
  vault_id: 1,
  size: 20,
  created_at: "2024-01-02",
  metadata: { status: "indexed", chunk_count: 2 },
  tags: [{ id: 10, name: "AlphaTag", document_count: 0 }],
};

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

async function flushMicrotasks(rounds = 8) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.listTags.mockResolvedValue(ALL_TAGS);
  apiMock.setDocumentTags.mockResolvedValue([]);
  apiMock.downloadDocument.mockResolvedValue(undefined);
});

describe("DocumentDetailPage preview race — committed preview survives a late stale load (critic probe, reversed vs C4)", () => {
  it("keeps doc2's heading AND committed TWO-CONTENT preview when the stale doc1 getDocument resolves last, without refetching blob 1", async () => {
    const d1 = deferred<any>();
    const d2 = deferred<any>();
    apiMock.getDocument.mockImplementation((id: any) =>
      Number(id) === 1 ? d1.promise : d2.promise
    );
    // Blob "2" resolves immediately so doc2's preview COMMITS; blob "1" never
    // settles — the stale document must never even START a preview fetch.
    apiMock.getDocumentRawBlob.mockImplementation((fid: any) =>
      String(fid) === "2"
        ? Promise.resolve(fakeBlob("TWO-CONTENT"))
        : new Promise<any>(() => {})
    );

    let utils!: ReturnType<typeof render>;
    await act(async () => {
      utils = render(<Harness />);
    });
    expect(apiMock.getDocument).toHaveBeenCalledWith(1); // load for id 1 in flight

    // Navigate to /documents/2 (same component instance) while id 1 is pending.
    await act(async () => {
      fireEvent.click(screen.getByTestId("nav-to-2"));
    });
    expect(apiMock.getDocument).toHaveBeenCalledWith(2); // load for id 2 in flight

    // The NEW document (2) resolves and its preview COMMITS — visible text.
    await act(async () => {
      d2.resolve(DOC2);
      await flushMicrotasks();
    });
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("two.txt");
    expect(apiMock.getDocumentRawBlob).toHaveBeenCalledWith("2", expect.anything());
    expect(utils.container.textContent).toContain("TWO-CONTENT");

    const blobCallsBeforeStale = apiMock.getDocumentRawBlob.mock.calls.length;

    // THEN the STALE getDocument(1) resolves late — after the current
    // document's preview has already committed.
    await act(async () => {
      d1.resolve(DOC1);
      await flushMicrotasks();
    });

    // (a) The heading still shows the current document, not the stale one.
    expect(
      screen.getByRole("heading", { level: 1 }),
      "stale getDocument(1) response must not overwrite the currently viewed document (two.txt)"
    ).toHaveTextContent("two.txt");

    // (b) The committed preview is STILL VISIBLE. Before the fix, the stale
    // resolution re-triggered the preview effect and its cleanup nulled
    // previewText — erasing the live preview even though the stale blob
    // itself could never commit.
    expect(
      utils.container.textContent,
      "the committed doc2 preview (TWO-CONTENT) must survive the late stale doc1 load"
    ).toContain("TWO-CONTENT");

    // (c) The stale load never re-triggers the preview effect: no additional
    // getDocumentRawBlob call, and in particular none for the stale id "1".
    expect(apiMock.getDocumentRawBlob.mock.calls.length).toBe(blobCallsBeforeStale);
    expect(
      apiMock.getDocumentRawBlob.mock.calls.every(([fid]) => String(fid) !== "1"),
      "the stale doc1 load must not start a new preview blob fetch"
    ).toBe(true);
  });
});

describe("DocumentDetailPage preview — in-flight blob request aborted on previewable→non-previewable navigation (F-001)", () => {
  it("aborts doc1's pending blob fetch when the user navigates to a non-previewable document", async () => {
    const PDF1 = { ...DOC1, filename: "one.pdf" };
    const DOCX2 = { ...DOC2, filename: "two.docx" };
    apiMock.getDocument.mockImplementation((id: any) =>
      Number(id) === 1 ? Promise.resolve(PDF1) : Promise.resolve(DOCX2)
    );
    // doc1's blob request never settles; capture its abort signal.
    let blob1Signal: AbortSignal | null = null;
    apiMock.getDocumentRawBlob.mockImplementation(
      (fid: unknown, signal: AbortSignal) => {
        if (String(fid) === "1") blob1Signal = signal;
        return new Promise<any>(() => {});
      }
    );

    await act(async () => {
      render(<Harness />);
      await flushMicrotasks();
    });
    expect(apiMock.getDocumentRawBlob).toHaveBeenCalledWith("1", expect.anything());
    expect(blob1Signal?.aborted).toBe(false);

    // Navigate to a .docx document: the NEW preview effect run early-returns
    // (non-previewable), so the PREVIOUS run's cleanup is the only teardown —
    // it must abort the still-pending blob request instead of letting it run
    // until unmount (F-001).
    await act(async () => {
      fireEvent.click(screen.getByTestId("nav-to-2"));
      await flushMicrotasks();
    });

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("two.docx");
    expect(
      blob1Signal?.aborted,
      "the doc1 blob request must be aborted after navigating away from it"
    ).toBe(true);
  });
});
