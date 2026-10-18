/**
 * Issue #718 (reviewer round-1 correction): pin the chunk-context request
 * WINDOW PARAMS. The frozen C2 check mocks `@/lib/api` wholesale, so nothing
 * pinned that `getChunkContext` actually asks the backend for neighbors —
 * deleting the params left every test green. This file spies on the real
 * axios client (no `@/lib/api` mock) and asserts the request shape.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const apiClientGet = vi.fn();

vi.mock("axios", () => ({
  default: {
    create: () => ({
      get: (...args: unknown[]) => {
        apiClientGet(...args);
        return { data: {} };
      },
      post: vi.fn(),
      put: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
      interceptors: {
        request: { use: vi.fn(), eject: vi.fn() },
        response: { use: vi.fn(), eject: vi.fn() },
      },
      defaults: { headers: { common: {} } },
    }),
  },
}));

import { getChunkContext } from "./core";

describe("getChunkContext window params (issue #718 AC2 mechanism)", () => {
  afterEach(() => {
    apiClientGet.mockClear();
  });

  it("requests a small neighbor window with the one-arg call", async () => {
    await getChunkContext("src-c13");
    expect(apiClientGet).toHaveBeenCalledTimes(1);
    const [url, config] = apiClientGet.mock.calls[0] as [
      string,
      { params?: Record<string, number> },
    ];
    expect(url).toBe("/search/chunks/src-c13/context");
    expect(config?.params).toEqual({ context_before: 1, context_after: 1 });
  });
});
