import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const axiosCreateMock = vi.hoisted(() => vi.fn());

vi.mock("axios", () => ({
  default: {
    create: axiosCreateMock,
  },
  create: axiosCreateMock,
}));

function mockAxiosClient() {
  return {
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
  };
}

describe("useAuthStore API base wiring", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("VITE_API_URL", "/knowledgevault/api");
    axiosCreateMock.mockReset();
    axiosCreateMock.mockImplementation(mockAxiosClient);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("builds both shared and auth axios clients from the shared API base", async () => {
    await import("./useAuthStore");

    const baseUrls = axiosCreateMock.mock.calls.map(([config]) => config.baseURL);
    expect(baseUrls).toEqual(["/knowledgevault/api", "/knowledgevault/api"]);
  });
});
