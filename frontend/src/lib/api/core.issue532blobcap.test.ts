// PRR-021 (PR #532): the Blob error-body decode in core.ts's CSRF response
// interceptor must be size-capped. Mirrors draftRoom.issue516.test.ts: the
// axios adapter is stubbed to reject with an axios-shaped error whose
// response data is a Blob, so the REAL interceptor chain runs. Two contracts:
//
//   (a) a small JSON Blob error body (<= MAX_ERROR_BLOB_DECODE_BYTES) is
//       still decoded, so parseDraftRoomError surfaces the backend
//       detail/code — the issue #516 API-003 behavior is preserved;
//   (b) a Blob error body ABOVE the cap is never buffered for decoding: the
//       Blob survives the interceptor chain untouched and the detail falls
//       back to the HTTP statusText. The oversized body is deliberately
//       VALID JSON — if the cap were missing, the decode would succeed and
//       the statusText fallback assertion below would fail.
import { afterEach, describe, expect, it, vi } from "vitest";

import { MAX_ERROR_BLOB_DECODE_BYTES, apiClient } from "./core";
import { exportDraftRevision, parseDraftRoomError } from "./draftRoom";

describe("blob error decode size cap (PRR-021 / PR #532)", () => {
  const originalAdapter = apiClient.defaults.adapter;

  afterEach(() => {
    apiClient.defaults.adapter = originalAdapter;
    vi.unstubAllGlobals();
  });

  function seedCsrf(): void {
    // core.ts's CSRF request interceptor runs before the adapter for POSTs.
    // Seed the cookie jar so ensureCsrfToken() resolves without a network
    // fetch; stub fetch as a belt-and-braces fallback.
    document.cookie = "X-CSRF-Token=test-csrf-token; path=/";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ csrf_token: "test-csrf-token" }),
      })
    );
  }

  function rejectWithBlobError(blob: Blob, status = 422): void {
    apiClient.defaults.adapter = (async () => {
      throw Object.assign(new Error(`Request failed with status code ${status}`), {
        isAxiosError: true,
        config: {
          method: "post",
          url: "/draft-room/drafts/1/revisions/2/export",
          headers: {},
        },
        response: {
          status,
          statusText: "Unprocessable Entity",
          data: blob,
          headers: {},
        },
      });
    }) as unknown as typeof apiClient.defaults.adapter;
  }

  async function catchExportError(): Promise<unknown> {
    try {
      await exportDraftRevision(1, 2, { format: "md" });
    } catch (error) {
      return error;
    }
    throw new Error("exportDraftRevision was expected to reject");
  }

  it("PRR-021a: a small JSON Blob 422 still decodes to detail/code (existing behavior preserved)", async () => {
    seedCsrf();
    const envelope = {
      detail: "Export blocked: oversized-cap regression fixture.",
      code: "fact_check_not_passed",
      context: { unresolved_findings: 1 },
    };
    const blob = new Blob([JSON.stringify(envelope)], { type: "application/json" });
    expect(blob.size).toBeLessThanOrEqual(MAX_ERROR_BLOB_DECODE_BYTES);

    rejectWithBlobError(blob);
    const caught = await catchExportError();
    expect(caught).toBeInstanceOf(Error);

    const info = parseDraftRoomError(caught);
    expect(info.detail).toBe(envelope.detail);
    expect(info.code).toBe(envelope.code);
    expect(info.status).toBe(422);
  });

  it("PRR-021b: a Blob at exactly the cap boundary still decodes (<= cap)", async () => {
    seedCsrf();
    const envelope = { detail: "boundary", code: "boundary_code" };
    // Solve for the exact pad length: every ASCII pad char adds exactly one
    // byte to the serialized body, so cap = serialized-length-with-empty-pad
    // + padLength.
    const skeleton = JSON.stringify({ ...envelope, context: { padding: "" } });
    const padLength = MAX_ERROR_BLOB_DECODE_BYTES - skeleton.length;
    expect(padLength).toBeGreaterThanOrEqual(0);
    const serialized = JSON.stringify({
      ...envelope,
      context: { padding: "x".repeat(padLength) },
    });
    const blob = new Blob([serialized], { type: "application/json" });
    expect(blob.size).toBe(MAX_ERROR_BLOB_DECODE_BYTES);

    rejectWithBlobError(blob);
    const caught = await catchExportError();

    const info = parseDraftRoomError(caught);
    expect(info.detail).toBe("boundary");
    expect(info.code).toBe("boundary_code");
  });

  it("PRR-021c: a >64KiB Blob error body skips decoding — Blob survives, detail falls back to statusText, nothing throws", async () => {
    seedCsrf();
    // Valid JSON but oversized: only the size cap can explain a skipped decode.
    const oversized = JSON.stringify({
      detail: "oversized body must NOT be decoded",
      code: "should_not_surface",
      context: { padding: "x".repeat(MAX_ERROR_BLOB_DECODE_BYTES) },
    });
    const blob = new Blob([oversized], { type: "application/json" });
    expect(blob.size).toBeGreaterThan(MAX_ERROR_BLOB_DECODE_BYTES);

    rejectWithBlobError(blob);
    const caught = await catchExportError();
    expect(caught).toBeInstanceOf(Error);

    // The interceptor must have left the Blob in place instead of buffering it.
    const data = (caught as { originalError?: { response?: { data?: unknown } } })
      .originalError?.response?.data;
    expect(data).toBeInstanceOf(Blob);

    const info = parseDraftRoomError(caught);
    expect(info.detail).toBe("Unprocessable Entity");
    expect(info.code).toBe("unknown");
    expect(info.status).toBe(422);
  });
});
