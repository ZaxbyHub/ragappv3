/**
 * Issue #515 acceptance checks — wiki race conditions, 409 draft loss,
 * terminal-job refresh, and ?page= deep links.
 *
 * AC31 — request identity: only the CURRENT list/detail request may commit
 *        state in useWikiData. A slow earlier response resolving LAST must
 *        never overwrite a newer one (list half and detail half).
 * AC32 — 409 conflict: a rejected updateWikiPage(409) must leave the edit
 *        dialog OPEN with the user's draft intact (the parent may toast).
 * AC33 — terminal job refresh keeps filters: the SSE-driven refetch must
 *        re-include the active search/page_type params.
 * AC39 — deep link: /wiki?page=42 loads page 42's detail without a click.
 *
 * All tests are DISCRIMINATING: they fail on the current tree because the
 * guarded behavior does not exist yet.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";
import React from "react";
import { MemoryRouter } from "react-router-dom";
import { toast } from "sonner";

// ---------------------------------------------------------------------------
// Mock API module — declared before component imports (hoisted).
// Mirrors the WikiPage.test.tsx mock surface; every export any mounted module
// needs is present so nothing falls through to the real axios client.
// ---------------------------------------------------------------------------
vi.mock("@/lib/api", () => ({
  listWikiPages: vi.fn(),
  getWikiPage: vi.fn(),
  createWikiPage: vi.fn(),
  updateWikiPage: vi.fn(),
  deleteWikiPage: vi.fn(),
  listWikiEntities: vi.fn().mockResolvedValue({ entities: [] }),
  listWikiClaims: vi.fn().mockResolvedValue({ claims: [] }),
  listWikiLintFindings: vi.fn().mockResolvedValue({ findings: [] }),
  runWikiLint: vi.fn().mockResolvedValue({ findings: [], count: 0 }),
  resolveWikiLintFinding: vi.fn().mockResolvedValue({}),
  searchWiki: vi.fn().mockResolvedValue({ pages: [], claims: [], entities: [], query: "" }),
  promoteMemoryToWiki: vi.fn(),
  updateMemory: vi.fn(),
  bulkWikiPageAction: vi.fn(),
  getWikiPageVersions: vi.fn().mockResolvedValue({ versions: [] }),
  getWikiPageFiles: vi.fn().mockResolvedValue({ files: [] }),
  getWikiPageBacklinks: vi.fn().mockResolvedValue({ backlinks: [] }),
  getWikiActivityFeed: vi.fn().mockResolvedValue([]),
  listWikiJobs: vi.fn().mockResolvedValue({ jobs: [] }),
  retryWikiJob: vi.fn(),
  cancelWikiJob: vi.fn(),
  recompileVaultWiki: vi.fn(),
  // useWikiEventStream reads these from the barrel.
  API_BASE_URL: "/api",
  getJwtAccessToken: vi.fn(() => null),
  refreshAccessToken: vi.fn(),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: () => ({ activeVaultId: 1 }),
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector">VaultSelector</div>,
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

// Radix Select cannot be driven in jsdom — WikiEditDialog is the only
// ui/select consumer mounted here. Mock the primitive with a context so the
// dialog renders and its text inputs stay real (WikiEditDialog.test.tsx
// pattern).
vi.mock("@/components/ui/select", async () => {
  const ReactMod = await import("react");
  const Ctx = ReactMod.createContext<(v: string) => void>(() => {});
  return {
    Select: ({ onValueChange, children }: any) =>
      ReactMod.createElement(Ctx.Provider, { value: onValueChange }, children),
    SelectTrigger: ({ children, id }: any) =>
      ReactMod.createElement("div", { role: "group", id }, children),
    SelectValue: ({ placeholder }: any) =>
      ReactMod.createElement("span", null, placeholder),
    SelectContent: ({ children }: any) => ReactMod.createElement("div", null, children),
    SelectItem: ({ value, children }: any) => {
      const onValueChange = ReactMod.useContext(Ctx);
      return ReactMod.createElement(
        "button",
        { type: "button", "data-select-item": value, onClick: () => onValueChange(value) },
        children,
      );
    },
  };
});

import WikiPage from "@/pages/WikiPage";
import { listWikiPages, getWikiPage, updateWikiPage } from "@/lib/api";

const listMock = vi.mocked(listWikiPages);
const getMock = vi.mocked(getWikiPage);
const updateMock = vi.mocked(updateWikiPage);

// WikiPage opens the wiki-events fetch stream on mount (useWikiEventStream).
// Stub fetch with an open (never-resolving) stream so no real request is made.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        body: {
          getReader: () => ({
            read: () => new Promise<{ value?: Uint8Array; done: boolean }>(() => {}),
            cancel: vi.fn(),
          }),
        },
      } as unknown as Response)
    )
  );
  // WikiEditDialog's toolbar restores the caret via rAF — run it sync.
  vi.stubGlobal(
    "requestAnimationFrame",
    (cb: FrameRequestCallback) => {
      cb(0);
      return 0;
    }
  );
  vi.clearAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function makeWikiPage(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    vault_id: 1,
    slug: "page-slug",
    title: "Page",
    page_type: "entity",
    markdown: "# Body",
    summary: "",
    status: "draft",
    confidence: 0,
    version: 1,
    created_by: null,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    last_compiled_at: null,
    claims: [],
    entities: [],
    lint_findings: [],
    ...overrides,
  };
}

function listResult(pages: Array<Record<string, unknown>>) {
  return { pages, page: 1, per_page: 50 };
}

const flush = () => act(async () => {});

// ---------------------------------------------------------------------------
// AC31 — request identity (list half + detail half)
// ---------------------------------------------------------------------------
describe("issue515 ac31", () => {
  it("issue515-ac31 stale responses never clobber newer list or detail state", async () => {
    // ---- list half: search A (slow) vs search B (fast) ----
    const listCalls: Array<{ params: any; d: Deferred<any> }> = [];
    listMock.mockImplementation((params: any) => {
      const d = deferred<any>();
      listCalls.push({ params, d });
      return d.promise;
    });

    const { unmount } = render(<WikiPage />);
    await flush();
    // WikiPage fires its [activeVaultId] effect AND its [activeType] effect on
    // mount, so more than one list request can be in flight — resolve them all
    // with the empty list before driving the searches.
    const mountCallCount = listCalls.length;
    expect(mountCallCount).toBeGreaterThanOrEqual(1);
    await act(async () => {
      for (let i = 0; i < mountCallCount; i++) {
        listCalls[i].d.resolve(listResult([]));
      }
    });

    const searchInput = screen.getByPlaceholderText("Search wiki...");
    fireEvent.change(searchInput, { target: { value: "alpha" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await flush();
    fireEvent.change(searchInput, { target: { value: "beta" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await flush();
    expect(listCalls.length).toBe(mountCallCount + 2); // mount calls + search A + search B

    // The fast search (B) resolves first and must be displayed...
    await act(async () => {
      listCalls[mountCallCount + 1].d.resolve(
        listResult([makeWikiPage({ id: 21, title: "Beta Fast Page", slug: "beta" })])
      );
    });
    expect(screen.getByText("Beta Fast Page")).toBeInTheDocument();

    // ...and the slow search (A) resolving LAST must NOT replace it.
    await act(async () => {
      listCalls[mountCallCount].d.resolve(
        listResult([makeWikiPage({ id: 11, title: "Alpha Slow Page", slug: "alpha" })])
      );
    });
    expect(screen.queryByText("Alpha Slow Page")).toBeNull();
    expect(screen.getByText("Beta Fast Page")).toBeInTheDocument();

    unmount();

    // ---- detail half: open page 1 (slow), then page 2 (fast) ----
    vi.clearAllMocks();
    listMock.mockImplementation(async () =>
      listResult([
        makeWikiPage({ id: 1, title: "Page One Slow", slug: "one" }),
        makeWikiPage({ id: 2, title: "Page Two Fast", slug: "two" }),
      ])
    );
    const detailDeferreds = new Map<number, Deferred<any>>();
    getMock.mockImplementation((pageId: number) => {
      const d = deferred<any>();
      detailDeferreds.set(pageId, d);
      return d.promise;
    });

    render(<WikiPage />);
    await waitFor(() => {
      expect(screen.getByText("Page One Slow")).toBeInTheDocument();
      expect(screen.getByText("Page Two Fast")).toBeInTheDocument();
    });

    // Open page 1 (slow detail), then page 2 (fast detail).
    fireEvent.click(screen.getByText("Page One Slow"));
    await flush();
    fireEvent.click(screen.getByText("Page Two Fast"));
    await flush();

    // Page 2 resolves first -> its detail must be the one displayed.
    await act(async () => {
      detailDeferreds.get(2)!.resolve(makeWikiPage({ id: 2, title: "Page Two Fast", slug: "two" }));
    });
    expect(screen.getByRole("heading", { name: "Page Two Fast" })).toBeInTheDocument();

    // Page 1 resolves LAST -> the displayed detail must stay page 2.
    await act(async () => {
      detailDeferreds.get(1)!.resolve(makeWikiPage({ id: 1, title: "Page One Slow", slug: "one" }));
    });
    expect(screen.getByRole("heading", { name: "Page Two Fast" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC32 — 409 conflict keeps the draft
// ---------------------------------------------------------------------------
describe("issue515 ac32", () => {
  it("issue515-ac32 409 conflict keeps edit dialog open with draft intact", async () => {
    listMock.mockResolvedValue(
      listResult([makeWikiPage({ id: 1, title: "Editable Page", slug: "editable" })])
    );
    getMock.mockResolvedValue(
      makeWikiPage({ id: 1, title: "Editable Page", slug: "editable", markdown: "Original body" })
    );
    updateMock.mockRejectedValue({ response: { status: 409 } });

    render(<WikiPage />);

    // Open the page, then its edit dialog (real WikiPageDetail header button).
    fireEvent.click(await screen.findByText("Editable Page"));
    await screen.findByRole("heading", { name: "Editable Page" });
    // Exact name "Edit": the list row Card also has role="button" and its
    // accessible name contains "Editable Page" (matches /edit/i).
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    const textarea = await screen.findByLabelText(/content \(markdown\)/i);
    fireEvent.change(textarea, { target: { value: "My precious draft" } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    // The parent surfaced the conflict (toast allowed)...
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalled();
    });

    // ...but the dialog must REMAIN OPEN with the draft still in it.
    const draft = screen.queryByLabelText(/content \(markdown\)/i);
    expect(draft).not.toBeNull();
    expect((draft as HTMLTextAreaElement).value).toBe("My precious draft");
  });
});

// ---------------------------------------------------------------------------
// AC33 — terminal job refresh keeps filters
// ---------------------------------------------------------------------------
describe("issue515 ac33", () => {
  // A controllable SSE body (WikiPage.sse.test.tsx pattern): `emit` pushes a
  // chunk to the reader; reads pend until a chunk is available.
  function controllableSse() {
    const encoder = new TextEncoder();
    let pending: ((r: { value?: Uint8Array; done: boolean }) => void) | null = null;
    const queue: Array<{ value?: Uint8Array; done: boolean }> = [];
    const reader = {
      read: vi.fn(
        () =>
          new Promise<{ value?: Uint8Array; done: boolean }>((resolve) => {
            if (queue.length) resolve(queue.shift()!);
            else pending = resolve;
          })
      ),
      cancel: vi.fn(),
    };
    const emit = (chunk: string) => {
      const item = { value: encoder.encode(chunk), done: false };
      if (pending) {
        const r = pending;
        pending = null;
        r(item);
      } else {
        queue.push(item);
      }
    };
    const response = {
      ok: true,
      status: 200,
      body: { getReader: () => reader },
    } as unknown as Response;
    return { response, emit };
  }

  it("issue515-ac33 terminal job refetch includes active search filters", async () => {
    listMock.mockResolvedValue(listResult([]));
    const sse = controllableSse();
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(sse.response);

    render(<WikiPage />);

    // Establish an active search.
    const searchInput = screen.getByPlaceholderText("Search wiki...");
    fireEvent.change(searchInput, { target: { value: "alpha" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(listMock).toHaveBeenCalledWith(
        expect.objectContaining({ vault_id: 1, search: "alpha" })
      );
    });
    listMock.mockClear();

    // Fire the job-terminal event over the stream.
    await act(async () => {
      sse.emit('data: {"type":"job_completed"}\n\n');
    });

    await waitFor(() => {
      expect(listMock).toHaveBeenCalled();
    });
    const lastParams = listMock.mock.calls[listMock.mock.calls.length - 1]?.[0];
    expect(lastParams).toEqual(
      expect.objectContaining({ vault_id: 1, search: "alpha" })
    );
  });
});

// ---------------------------------------------------------------------------
// AC39 — /wiki?page=<id> deep link
// ---------------------------------------------------------------------------
describe("issue515 ac39", () => {
  it("issue515-ac39 page query param deep link loads that page detail", async () => {
    listMock.mockResolvedValue(listResult([]));
    getMock.mockImplementation(async (pageId: number) =>
      makeWikiPage({ id: pageId, title: "Deep Linked Page", slug: "deep" })
    );

    render(
      <MemoryRouter initialEntries={["/wiki?page=42"]}>
        <WikiPage />
      </MemoryRouter>
    );

    // The detail for page 42 must load WITHOUT clicking anything in the list.
    await waitFor(
      () => {
        expect(getMock).toHaveBeenCalledWith(42);
      },
      { timeout: 2000 }
    );
    expect(await screen.findByRole("heading", { name: "Deep Linked Page" })).toBeInTheDocument();

    // NOTE: history/popstate restoration (Back from the detail to the list) is
    // exercised implicitly by the same search-param wiring this test forces;
    // not separately asserted here.
  });
});
