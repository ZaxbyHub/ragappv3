// frontend/src/lib/api/core.document-statuses.test.ts
// Issue #514 / FU-008: the batched status client pages id sets larger than the
// server's per-request cap through multiple GET /documents/status requests and
// merges the envelopes — a >100-file bulk upload must keep monitoring instead
// of failing the whole poll with 400.
//
// core.ts builds its own axios singleton (`apiClient`), so the client under
// test is observable by spying on the exported instance's `get` — the same
// module-instance the endpoints close over. No network is ever attempted
// because the spy replaces the request entry point entirely.

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AxiosResponse } from "axios";
import { apiClient, BATCHED_STATUS_MAX_IDS, getDocumentStatuses } from "./core";

/** Minimal AxiosResponse carrier for the mocked GET body. */
function response(data: unknown): AxiosResponse {
  return { data } as AxiosResponse;
}

const getSpy = vi.spyOn(apiClient, "get");

beforeEach(() => {
  getSpy.mockReset();
});

describe("getDocumentStatuses (issue #514 — chunked batched status)", () => {
  it("sends ≤100 ids as exactly ONE GET with a comma-joined ids param and the vault_id", async () => {
    getSpy.mockResolvedValueOnce(
      response({ results: [{ id: 1, status: "processing", chunk_count: 0 }] })
    );

    const merged = await getDocumentStatuses([1, 2, 3], 5);

    expect(getSpy).toHaveBeenCalledTimes(1);
    expect(getSpy).toHaveBeenCalledWith("/documents/status", {
      params: { ids: "1,2,3", vault_id: 5 },
    });
    expect(merged.results).toEqual([
      { id: 1, status: "processing", chunk_count: 0 },
    ]);
    // No per-id errors anywhere: `errors` stays absent, not an empty array.
    expect(merged.errors).toBeUndefined();
  });

  it("omits vault_id entirely when no vault is provided", async () => {
    getSpy.mockResolvedValueOnce(response({ results: [] }));

    await getDocumentStatuses([9]);

    expect(getSpy).toHaveBeenCalledTimes(1);
    // Exact-match the config so a stray vault_id key would fail the test.
    expect(getSpy).toHaveBeenCalledWith("/documents/status", {
      params: { ids: "9" },
    });
  });

  it("splits 250 ids into 100/100/50 requests and merges results and errors in slice order", async () => {
    const ids = Array.from({ length: 250 }, (_, i) => i);
    getSpy
      .mockResolvedValueOnce(
        response({ results: [{ id: 1, status: "processing", chunk_count: 0 }] })
      )
      .mockResolvedValueOnce(
        response({
          results: [{ id: 2, status: "processing", chunk_count: 0 }],
          errors: [{ id: 150, error: "unknown document" }],
        })
      )
      .mockResolvedValueOnce(
        response({ results: [{ id: 3, status: "indexed", chunk_count: 7 }] })
      );

    const merged = await getDocumentStatuses(ids);

    expect(getSpy).toHaveBeenCalledTimes(3);
    const paramIds = getSpy.mock.calls.map(
      (call) => (call[1] as { params: { ids: string } }).params.ids.split(",")
    );
    // Slice boundaries: 100 / 100 / 50, in request order.
    expect(paramIds.map((parts) => parts.length)).toEqual([100, 100, 50]);
    expect(paramIds[0][0]).toBe("0");
    expect(paramIds[0][99]).toBe("99");
    expect(paramIds[1][0]).toBe("100");
    expect(paramIds[1][99]).toBe("199");
    expect(paramIds[2][0]).toBe("200");
    expect(paramIds[2][49]).toBe("249");

    // Merged results concatenate in slice order.
    expect(merged.results.map((entry) => entry.id)).toEqual([1, 2, 3]);
    // Per-id errors from ANY slice are concatenated into the merged envelope.
    expect(merged.errors).toEqual([{ id: 150, error: "unknown document" }]);
  });

  it("keeps `errors` undefined when no slice reports per-id errors", async () => {
    const ids = Array.from({ length: 150 }, (_, i) => 1000 + i);
    getSpy
      .mockResolvedValueOnce(
        response({ results: [{ id: 1000, status: "processing", chunk_count: 0 }] })
      )
      .mockResolvedValueOnce(
        response({ results: [{ id: 1042, status: "processing", chunk_count: 0 }] })
      );

    const merged = await getDocumentStatuses(ids);

    // 150 ids cross the 100-id cap exactly once: 100 + 50.
    expect(getSpy).toHaveBeenCalledTimes(2);
    expect(merged.results.map((entry) => entry.id)).toEqual([1000, 1042]);
    expect(merged.errors).toBeUndefined();
  });
});

describe("BATCHED_STATUS_MAX_IDS", () => {
  it("exports the server's 100-id per-request cap", () => {
    expect(BATCHED_STATUS_MAX_IDS).toBe(100);
  });
});
