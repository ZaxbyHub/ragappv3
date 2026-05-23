import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchPagesMock = vi.hoisted(() => vi.fn());
const fetchLintFindingsMock = vi.hoisted(() => vi.fn());
const closeEventSourceMock = vi.hoisted(() => vi.fn());
const eventSourceCalls = vi.hoisted(
  () => [] as Array<{ url: string; options: EventSourceInit }>
);

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

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("./WikiPageList", () => ({
  WikiPageList: () => <div data-testid="wiki-list" />,
}));

vi.mock("./WikiPageDetail", () => ({
  WikiPageDetail: () => <div data-testid="wiki-detail" />,
}));

vi.mock("./WikiEditDialog", () => ({
  WikiEditDialog: () => <div data-testid="wiki-edit" />,
}));

vi.mock("./WikiLintPanel", () => ({
  WikiLintPanel: () => <div data-testid="wiki-lint" />,
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

class MockEventSource {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(url: string, options: EventSourceInit) {
    eventSourceCalls.push({ url, options });
  }

  close() {
    closeEventSourceMock();
  }
}

describe("WikiPage SSE path wiring", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("VITE_API_URL", "/knowledgevault/api");
    eventSourceCalls.length = 0;
    fetchPagesMock.mockReset();
    fetchLintFindingsMock.mockReset();
    closeEventSourceMock.mockReset();
    vi.stubGlobal("EventSource", MockEventSource);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("subscribes to wiki events through the shared public API base", async () => {
    const { default: WikiPage } = await import("./WikiPage");

    const { unmount } = render(<WikiPage />);

    await waitFor(() => expect(eventSourceCalls).toHaveLength(1));
    expect(eventSourceCalls[0]).toEqual({
      url: "/knowledgevault/api/wiki/events?vault_id=42",
      options: { withCredentials: true },
    });

    unmount();
    expect(closeEventSourceMock).toHaveBeenCalledTimes(1);
  });
});
