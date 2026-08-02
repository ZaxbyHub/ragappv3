import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DraftRoomCapabilities } from "@/lib/api/draftRoom";

const getDraftRoomCapabilitiesMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/draftRoom", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/draftRoom")>("@/lib/api/draftRoom");
  return {
    ...actual,
    getDraftRoomCapabilities: getDraftRoomCapabilitiesMock,
  };
});

import { useDraftRoomCapabilities, useDraftRoomVisible } from "./useDraftRoomCapabilities";
import { draftRoomKeys } from "@/lib/api/draftRoom";

function makeCapabilities(overrides: Partial<DraftRoomCapabilities> = {}): DraftRoomCapabilities {
  return {
    enabled: true,
    modes: ["compose", "rewrite"],
    tiers: ["high_stakes", "sensitive", "standard"],
    piece_types: ["article", "report", "brief", "press_release", "other"],
    transformation_strengths: ["light", "moderate", "substantial"],
    limits: {},
    export_formats: ["md"],
    logical_model_modes: ["instant", "thinking"],
    default_logical_mode: "instant",
    compile_start_stages: ["research", "outline", "draft", "lint", "copy", "standards", "fact"],
    compile_stage_order: [
      "intake",
      "research",
      "outline",
      "draft",
      "lint",
      "copy",
      "standards",
      "fact",
      "assemble",
    ],
    prompt_bundle_version: "v1",
    editorial_gates_installed: true,
    compile_available: true,
    findings_available: true,
    claims_available: true,
    evidence_available: true,
    ready_available: true,
    promote_available: false,
    ...overrides,
  };
}

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

describe("useDraftRoomCapabilities", () => {
  afterEach(() => {
    cleanup();
    getDraftRoomCapabilitiesMock.mockReset();
  });

  it("fetches capabilities with the canonical query key", async () => {
    getDraftRoomCapabilitiesMock.mockResolvedValue(makeCapabilities());
    const { result } = renderHook(() => useDraftRoomCapabilities(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.enabled).toBe(true);
    expect(getDraftRoomCapabilitiesMock).toHaveBeenCalledTimes(1);
  });

  it("exposes the query under draftRoomKeys.capabilities()", () => {
    expect(draftRoomKeys.capabilities()).toEqual(["draft-room", "capabilities"]);
  });
});

describe("useDraftRoomVisible", () => {
  afterEach(() => {
    cleanup();
    getDraftRoomCapabilitiesMock.mockReset();
  });

  it("returns false while the capabilities query is loading", () => {
    getDraftRoomCapabilitiesMock.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useDraftRoomVisible(), { wrapper });
    expect(result.current).toBe(false);
  });

  it("returns false on a failed capabilities fetch", async () => {
    getDraftRoomCapabilitiesMock.mockRejectedValue(new Error("network error"));
    const { result } = renderHook(() => useDraftRoomVisible(), { wrapper });
    await waitFor(() => expect(result.current).toBe(false));
  });

  it("returns false when the backend reports the feature disabled", async () => {
    getDraftRoomCapabilitiesMock.mockResolvedValue(makeCapabilities({ enabled: false }));
    const { result } = renderHook(() => useDraftRoomVisible(), { wrapper });
    await waitFor(() => expect(getDraftRoomCapabilitiesMock).toHaveBeenCalled());
    // Give the query a tick to settle into success state.
    await waitFor(() => expect(result.current).toBe(false));
  });

  it("returns false when editorial_gates_installed is false", async () => {
    getDraftRoomCapabilitiesMock.mockResolvedValue(makeCapabilities({ editorial_gates_installed: false }));
    const { result } = renderHook(() => useDraftRoomVisible(), { wrapper });
    await waitFor(() => expect(getDraftRoomCapabilitiesMock).toHaveBeenCalled());
    await waitFor(() => expect(result.current).toBe(false));
  });

  it("returns false when ready_available is false", async () => {
    getDraftRoomCapabilitiesMock.mockResolvedValue(makeCapabilities({ ready_available: false }));
    const { result } = renderHook(() => useDraftRoomVisible(), { wrapper });
    await waitFor(() => expect(getDraftRoomCapabilitiesMock).toHaveBeenCalled());
    await waitFor(() => expect(result.current).toBe(false));
  });

  it("returns true only when enabled, editorial_gates_installed, and ready_available all hold", async () => {
    getDraftRoomCapabilitiesMock.mockResolvedValue(makeCapabilities());
    const { result } = renderHook(() => useDraftRoomVisible(), { wrapper });
    await waitFor(() => expect(result.current).toBe(true));
  });
});
