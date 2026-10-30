/**
 * DocumentDetailPage K765 slow-load regression pin (issue #765 AC7; NOT part
 * of the frozen checkpoint set — added at the implementation reviewer's
 * direction after it caught the refresh-disarm race this file pins).
 *
 * The background status refresh must SURVIVE its first tick firing before the
 * foreground load commits: a tick with no loaded document (or the previous
 * document still in the mirror) re-arms instead of returning, so a load that
 * resolves AFTER the first 2 s tick still converges a processing document to
 * indexed without a remount. At the pre-race-fix head the loop died on that
 * first tick and the page stayed "Processing" — the exact symptom #765 AC7
 * forbids. RED at the race head, GREEN after the fix.
 *
 * Harness mirrors DocumentDetailPage.k765.test.tsx (proven in this repo).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act, screen, fireEvent } from "@testing-library/react";
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
import { useUploadStore } from "@/stores/useUploadStore";

function fakeBlob(text: string) {
  return { text: async () => text };
}

const DOC = {
  id: "1",
  filename: "slow.txt",
  vault_id: 1,
  size: 10,
  created_at: "2024-01-01",
  metadata: { status: "processing", chunk_count: 0 },
  tags: [],
};

function Harness() {
  return (
    <MemoryRouter initialEntries={["/documents/1"]}>
      <Routes>
        <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
      </Routes>
      {/* A real in-router navigation to doc 2: the SAME page instance stays
       * mounted (only the :documentId param changes), so component refs —
       * including the status-refresh in-flight flag — survive the change. */}
      <Link to="/documents/2">navigate-to-doc-2</Link>
    </MemoryRouter>
  );
}

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

describe("DocumentDetailPage K765 slow-load regression", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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

  it("converges a processing document whose first load resolves after the first refresh tick", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    // The foreground load is HELD: it resolves processing only after the
    // first 2 s background tick has already fired into an empty doc mirror.
    let releaseLoad!: (value: unknown) => void;
    const heldLoad = new Promise((resolve) => {
      releaseLoad = resolve;
    });
    apiMock.getDocument.mockImplementationOnce(() => heldLoad as never);
    // Every later read (the background refreshes) resolves indexed.
    apiMock.getDocument.mockImplementation(() =>
      Promise.resolve({
        ...DOC,
        metadata: { status: "indexed", chunk_count: 3 },
      }),
    );

    render(<Harness />);
    // Fire the first tick BEFORE the load commits (2 s), then release the
    // load (3 s) — at the race head the loop is already dead by now.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await act(async () => {
      releaseLoad({ ...DOC });
      await flushMicrotasks();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    // Sanity: the foreground load committed the processing document.
    expect(screen.getByRole("button", { name: /ask about this document/i })).toBeDisabled();

    // The page must converge without any remount: the re-armed background
    // refresh observes indexed and enables the Ask affordance.
    for (let index = 0; index < 10; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
    }
    expect(
      screen.getByRole("button", { name: /ask about this document/i }),
      "the status refresh must re-arm when its first tick precedes the load",
    ).toBeEnabled();
  });

  it("the refresh loop survives a route change while the previous document's refresh is in flight", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    // Doc 1's FOREGROUND load resolves processing; its first BACKGROUND
    // refresh (the 2 s tick) is HELD in flight across the navigation (review
    // round 2 PRR-001 — Copilot/shard A and the critic independently
    // reproduced the wedge pre-fix). Doc 2 stays processing through its first
    // refresh, then converges to indexed.
    const callsPerDoc: Record<number, number> = {};
    let releaseDocOneRefresh!: (value: unknown) => void;
    const docOneRefresh = new Promise((resolve) => {
      releaseDocOneRefresh = resolve;
    });
    const docTwo = (status: string) => ({
      ...DOC,
      id: "2",
      filename: "two.txt",
      metadata: { status, chunk_count: status === "indexed" ? 3 : 0 },
    });
    apiMock.getDocument.mockImplementation(((id: number) => {
      callsPerDoc[id] = (callsPerDoc[id] ?? 0) + 1;
      if (id === 1) {
        return callsPerDoc[1] === 1
          ? Promise.resolve({ ...DOC })
          : (docOneRefresh as never);
      }
      return Promise.resolve(docTwo(callsPerDoc[2] <= 2 ? "processing" : "indexed"));
    }) as never);

    render(<Harness />);
    await act(async () => {
      await flushMicrotasks();
    });
    expect(screen.getByRole("button", { name: /ask about this document/i })).toBeDisabled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    // Sentinel: doc 1's background refresh dispatched and is HELD in flight.
    expect(callsPerDoc[1]).toBe(2);

    // Route change to doc 2 on the SAME instance: the leftover in-flight
    // flag is exactly what the pre-fix code never reset.
    await act(async () => {
      fireEvent.click(screen.getByText("navigate-to-doc-2"));
    });
    await act(async () => {
      await flushMicrotasks();
    });
    expect(callsPerDoc[2]).toBe(1);
    expect(screen.getByRole("button", { name: /ask about this document/i })).toBeDisabled();

    // Doc 2's first refresh tick: still processing (the loop is alive).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(callsPerDoc[2]).toBe(2);

    // Doc 1's held refresh settles into its disposed closure DURING doc 2's
    // polling window. Pre-fix the loop died at doc 2's first tick (the bare
    // in-flight return left no successor timer); post-fix the effect-run
    // reset means this settle is harmless and doc 2 keeps ticking.
    await act(async () => {
      releaseDocOneRefresh({ ...DOC, metadata: { status: "indexed", chunk_count: 3 } });
      await flushMicrotasks();
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    // Discriminating assertion: doc 2's loop kept ticking and converged.
    expect(callsPerDoc[2]).toBe(3);
    expect(callsPerDoc[1]).toBe(2);
    expect(
      screen.getByRole("button", { name: /ask about this document/i }),
      "the new document's refresh loop must not inherit the previous document's in-flight state",
    ).toBeEnabled();
  });

  it("the status loop stops after repeated refresh failures", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    // The foreground load succeeds (processing); EVERY background refresh
    // rejects — a deleted document or a persistently failing endpoint
    // (review round 2 PRR-010: the loop must terminate, not poll forever).
    apiMock.getDocument.mockImplementationOnce(() => Promise.resolve({ ...DOC }) as never);
    apiMock.getDocument.mockRejectedValue(new Error("document gone"));

    render(<Harness />);
    await act(async () => {
      await flushMicrotasks();
    });
    // Sentinel: exactly the foreground load so far; the doc is processing.
    expect(apiMock.getDocument).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /ask about this document/i })).toBeDisabled();

    // 24 s of 2 s ticks: the five permitted failures land (ticks at 2..10 s)
    // and the 6th tick (12 s) disarms the loop — nothing fires after it.
    for (let index = 0; index < 12; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
    }
    expect(apiMock.getDocument).toHaveBeenCalledTimes(6);

    for (let index = 0; index < 4; index += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
    }
    // Discriminating assertion: 1 initial load + 5 refreshes and NOT ONE
    // more — at base a 404ing document was re-requested every 2 s forever.
    expect(apiMock.getDocument).toHaveBeenCalledTimes(6);
  });

  it("the background refresh merges top-level progress fields into the rendered document", { timeout: 30_000 }, async () => {
    vi.useFakeTimers({
      toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    // B-5 (Multi-Stage round 1): the merge must carry the production
    // top-level progress surface (progress_percent etc.), not only metadata
    // fields — the Details card renders it as a live Progress cell.
    apiMock.getDocument.mockResolvedValueOnce({
      ...DOC,
      metadata: { status: "processing", chunk_count: 0 },
      progress_percent: 10,
    } as never);
    apiMock.getDocument.mockImplementation(() =>
      Promise.resolve({
        ...DOC,
        metadata: { status: "processing", chunk_count: 0 },
        progress_percent: 55,
      } as never),
    );

    render(<Harness />);
    await act(async () => {
      await flushMicrotasks();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500);
    });

    // Discriminating assertion: the merged doc carries the REFRESHED
    // top-level field. At the pre-B-5 head the merge copied only
    // metadata.status/chunk_count/progress, so the rendered Progress cell
    // stayed at the foreground load's 10% forever.
    expect(
      screen.getByText("55%"),
      "the background refresh must merge top-level progress_percent into the rendered document",
    ).toBeInTheDocument();
    expect(screen.queryByText("10%")).not.toBeInTheDocument();
  });
});
