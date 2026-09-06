import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CanvasCapabilities } from "@/lib/api/canvas";

const getCanvasCapabilitiesMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/canvas", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/canvas")>("@/lib/api/canvas");
  return {
    ...actual,
    getCanvasCapabilities: getCanvasCapabilitiesMock,
  };
});

import { useCanvasCapabilities, useCanvasVisible } from "./useCanvasCapabilities";
import { canvasKeys } from "@/lib/api/canvas";

function makeCapabilities(overrides: Partial<CanvasCapabilities> = {}): CanvasCapabilities {
  return { enabled: true, ...overrides };
}

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

describe("useCanvasCapabilities", () => {
  afterEach(() => {
    cleanup();
    getCanvasCapabilitiesMock.mockReset();
  });

  it("fetches capabilities with the canonical query key", async () => {
    getCanvasCapabilitiesMock.mockResolvedValue(makeCapabilities());
    const { result } = renderHook(() => useCanvasCapabilities(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.enabled).toBe(true);
    expect(getCanvasCapabilitiesMock).toHaveBeenCalledTimes(1);
  });

  it("exposes the query under canvasKeys.capabilities()", () => {
    expect(canvasKeys.capabilities()).toEqual(["canvas", "capabilities"]);
  });
});

describe("useCanvasVisible (fail-closed)", () => {
  afterEach(() => {
    cleanup();
    getCanvasCapabilitiesMock.mockReset();
  });

  it("returns false while the capabilities query is loading", () => {
    getCanvasCapabilitiesMock.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useCanvasVisible(), { wrapper });
    expect(result.current).toBe(false);
  });

  it("returns false when the backend signals disabled via a 503 error", async () => {
    // The backend returns 503 canvas_disabled rather than {enabled:false}.
    const error = new Error("canvas_disabled");
    (error as unknown as { status?: number }).status = 503;
    getCanvasCapabilitiesMock.mockRejectedValue(error);
    const { result } = renderHook(() => useCanvasVisible(), { wrapper });
    await waitFor(() => expect(result.current).toBe(false));
  });

  it("returns false when the payload reports enabled: false", async () => {
    getCanvasCapabilitiesMock.mockResolvedValue(makeCapabilities({ enabled: false }));
    const { result } = renderHook(() => useCanvasVisible(), { wrapper });
    await waitFor(() => expect(result.current).toBe(false));
  });

  it("returns true only when the payload reports enabled: true", async () => {
    getCanvasCapabilitiesMock.mockResolvedValue(makeCapabilities());
    const { result } = renderHook(() => useCanvasVisible(), { wrapper });
    await waitFor(() => expect(result.current).toBe(true));
  });
});
