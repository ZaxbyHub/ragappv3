/**
 * DocumentDetailPage K765 acceptance checks (issue #765, frozen).
 *
 *   C7 (DISCRIMINATING) — a PROCESSING document must converge to indexed on
 *   the detail page WITHOUT a remount. At base the page loads exactly once
 *   per route id: a document that was processing at load time stays
 *   processing forever, so the Ask affordance stays disabled even after the
 *   backend finished indexing. RED at base, GREEN after the fix.
 *
 *   C8 (DISCRIMINATING) — the Ask affordance's tooltip for a FAILED
 *   document must not claim the document is merely "waiting" to be indexed.
 *   At base every non-searchable document (processing, error, pending)
 *   shares the "Waiting until indexed" tooltip. RED at base, GREEN after
 *   the fix.
 *
 * Mock/wiring style mirrors DocumentDetailPage.preview-race.test.tsx
 * (proven in this repo): the vi.hoisted "@/lib/api" mock, the sonner
 * callable toast mock, in-router rendering, and the REAL useUploadMonitoring
 * hook over an upload store that holds no entry for this document.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act, screen } from "@testing-library/react";
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

import DocumentDetailPage from "@/pages/DocumentDetailPage";
import { useUploadStore } from "@/stores/useUploadStore";

function fakeBlob(text: string) {
  return { text: async () => text };
}

const DOC_BASE = {
  id: "1",
  filename: "one.txt",
  vault_id: 1,
  size: 10,
  created_at: "2024-01-01",
  metadata: { status: "indexed", chunk_count: 1 },
  tags: [],
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

describe("DocumentDetailPage K765 acceptance checks", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The upload store holds NO entry for this document: the upload monitor
    // stays disarmed and cannot drive any convergence (the page itself must).
    useUploadStore.setState({ uploads: [] });
    apiMock.listTags.mockResolvedValue([]);
    apiMock.setDocumentTags.mockResolvedValue([]);
    apiMock.downloadDocument.mockResolvedValue(undefined);
    apiMock.getDocumentRawBlob.mockImplementation(() =>
      Promise.resolve(fakeBlob("PREVIEW-CONTENT")),
    );
  });

  afterEach(async () => {
    if (vi.isFakeTimers()) vi.clearAllTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
    await act(async () => {
      await flushMicrotasks();
    });
  });

  it("processing document converges to indexed without a remount", { timeout: 60_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    let docCalls = 0;
    apiMock.getDocument.mockImplementation(() => {
      docCalls += 1;
      const status = docCalls === 1 ? "processing" : "indexed";
      return Promise.resolve({
        ...DOC_BASE,
        metadata: { status, chunk_count: 2 },
      });
    });

    await act(async () => {
      render(<Harness />);
      await flushMicrotasks();
    });

    // Sentinel: the first load rendered the processing document and the Ask
    // affordance is disabled for it.
    const initially = screen.getByRole("button", {
      name: /ask about this document/i,
    }) as HTMLButtonElement;
    expect(initially.disabled).toBe(true);
    expect(apiMock.getDocument).toHaveBeenCalledTimes(1);

    for (let step = 0; step < 30; step += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
    }

    // Discriminating assertion: 60s later the page must have re-loaded the
    // document (the backend has since indexed it) and converged WITHOUT a
    // remount — the Ask affordance is enabled. At base the page never
    // re-loads: the document stays "processing" and the button stays
    // disabled.
    // RED at base, GREEN after the fix.
    const ask = screen.getByRole("button", {
      name: /ask about this document/i,
    }) as HTMLButtonElement;
    expect(ask.disabled).toBe(false);

    // Terminal-disarm pin (review round 1): once converged to a TERMINAL
    // status the refresh loop must STOP — 20s more of ticks issue no further
    // getDocument calls (an unarmed loop would keep polling forever).
    const callsAtConvergence = apiMock.getDocument.mock.calls.length;
    for (let step = 0; step < 10; step += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
    }
    expect(apiMock.getDocument).toHaveBeenCalledTimes(callsAtConvergence);
  });

  it("failed document Ask tooltip does not say waiting until indexed", { timeout: 30_000 }, async () => {
    apiMock.getDocument.mockResolvedValue({
      ...DOC_BASE,
      metadata: { status: "error", chunk_count: 0, error_message: "extraction crashed" },
    });

    await act(async () => {
      render(<Harness />);
      await flushMicrotasks();
    });

    const ask = screen.getByRole("button", {
      name: /ask about this document/i,
    }) as HTMLButtonElement;
    // Sentinel: the failed document is indeed not searchable (the button is
    // disabled) — the check is about the tooltip, not the gate.
    expect(ask.disabled).toBe(true);

    // Discriminating assertion: a document whose indexing FAILED is not
    // "waiting" for anything — the tooltip must not claim it is. At base the
    // shared non-searchable tooltip reads "Waiting until indexed — ...".
    // RED at base, GREEN after the fix.
    expect(ask.title).not.toContain("Waiting until indexed");
    // Positive pins (review round 1): the tooltip states the FAILURE, and
    // the same description renders as VISIBLE text in the Details card —
    // not only an unreachable title on a pointer-events-none button.
    expect(ask.title.startsWith("Indexing failed")).toBe(true);
    expect(
      screen.getByText("Indexing failed — extraction crashed. Re-upload the document to index it again."),
    ).toBeInTheDocument();
  });

  it("cancelled document Ask tooltip says indexing was cancelled", { timeout: 30_000 }, async () => {
    apiMock.getDocument.mockResolvedValue({
      ...DOC_BASE,
      metadata: { status: "cancelled", chunk_count: 0 },
    });

    await act(async () => {
      render(<Harness />);
      await flushMicrotasks();
    });

    const ask = screen.getByRole("button", {
      name: /ask about this document/i,
    }) as HTMLButtonElement;
    // Sentinel: a cancelled document is not searchable either.
    expect(ask.disabled).toBe(true);

    // Discriminating assertion (cancelled branch): the tooltip names the
    // cancellation — it is not "waiting", and not the error copy either.
    expect(ask.title).toContain("Indexing was cancelled");
    expect(ask.title).not.toContain("Waiting until indexed");
    expect(ask.title).not.toContain("Indexing failed");
    // The visible description renders (no failureMessage on this fixture).
    expect(
      screen.getByText("Indexing was cancelled. Re-upload the document to index it again."),
    ).toBeInTheDocument();
  });
});
