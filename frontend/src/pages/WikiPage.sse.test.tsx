import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// WikiPage subscribes to the wiki compile-job stream via an authenticated
// `fetch` (Bearer header), NOT EventSource — EventSource cannot send an
// Authorization header and the app sets no access-token cookie, so an
// EventSource subscription always 401s. These tests assert the fetch-stream
// wiring: the public API base + Bearer header are used, a terminal job event
// drives the refetch callbacks, and the stream is aborted on unmount.

const fetchPagesMock = vi.hoisted(() => vi.fn());
const fetchLintFindingsMock = vi.hoisted(() => vi.fn());

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: () => ({ activeVaultId: 42 }),
}));

vi.mock("@/hooks/useWikiData", () => ({
  useWikiData: () => ({
    pages: [],
    selectedPage: null,
    lintFindings: [],
    loading: false,
    error: null,
    fetchPages: fetchPagesMock,
    openPage: vi.fn(),
    closePage: vi.fn(),
    createPage: vi.fn(),
    editPage: vi.fn(),
    removePage: vi.fn(),
    fetchLintFindings: fetchLintFindingsMock,
    runLint: vi.fn().mockResolvedValue([]),
  }),
}));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/knowledgevault/api",
  getJwtAccessToken: () => "test-jwt-token",
  refreshAccessToken: vi.fn(),
  getWikiActivityFeed: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("./WikiPageList", () => ({
  WikiPageList: () => <div data-testid="wiki-list" />,
  PAGE_TYPES: [
    { value: "", label: "All" },
    { value: "overview", label: "Overview" },
    { value: "entity", label: "Entities" },
  ],
}));

vi.mock("./WikiPageDetail", () => ({
  WikiPageDetail: () => <div data-testid="wiki-detail" />,
}));

vi.mock("./WikiEditDialog", () => ({
  WikiEditDialog: () => <div data-testid="wiki-edit" />,
}));

vi.mock("./WikiLintPanel", () => ({
  WikiLintPanel: ({ vaultId: _v }: { vaultId: number | null }) => <div data-testid="wiki-lint" />,
}));

vi.mock("./WikiJobsPanel", () => ({
  WikiJobsPanel: () => <div data-testid="wiki-jobs" />,
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    info: vi.fn(),
  },
}));

// A controllable SSE body: `emit` pushes a chunk to the reader; reads pend
// until a chunk is available, mirroring a live (open) SSE connection.
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

describe("WikiPage SSE path wiring (fetch + Bearer)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    fetchPagesMock.mockReset();
    fetchLintFindingsMock.mockReset();
    fetchMock = vi.fn().mockResolvedValue(controllableSse().response);
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens the stream through the shared API base with a Bearer header", async () => {
    const { default: WikiPage } = await import("./WikiPage");

    render(<WikiPage />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/knowledgevault/api/wiki/events?vault_id=42");
    expect(init.method).toBe("GET");
    expect(init.headers.Authorization).toBe("Bearer test-jwt-token");
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("refetches pages and lint findings on a job_completed event", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const { default: WikiPage } = await import("./WikiPage");

    render(<WikiPage />);

    // Wait for the stream to open, then clear the mount-time refetch calls so
    // the assertion isolates the SSE-driven refetch.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetchPagesMock).toHaveBeenCalled());
    fetchPagesMock.mockClear();
    fetchLintFindingsMock.mockClear();

    emit('data: {"type":"job_completed"}\n\n');

    await waitFor(() => {
      expect(fetchPagesMock).toHaveBeenCalled();
      expect(fetchLintFindingsMock).toHaveBeenCalled();
    });
  });

  it("aborts the stream on unmount", async () => {
    let capturedSignal: AbortSignal | undefined;
    fetchMock.mockImplementation((_url: string, init: RequestInit) => {
      capturedSignal = init.signal as AbortSignal;
      return Promise.resolve(controllableSse().response);
    });
    const { default: WikiPage } = await import("./WikiPage");

    const { unmount } = render(<WikiPage />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    expect(capturedSignal?.aborted).toBe(false);
    unmount();
    expect(capturedSignal?.aborted).toBe(true);
  });
});
