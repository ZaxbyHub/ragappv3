// Regression test for issue #516 acceptance check AC21 (API-003 blob error
// decode). Unlike draftRoom.test.ts (which mocks the whole ./core module for
// pure endpoint-dispatch checks), this file exercises the REAL core.ts
// interceptor chain: the axios adapter is stubbed to reject with an axios-
// shaped error whose response data is a Blob containing JSON — exactly what
// axios delivers when a `responseType: "blob"` request (the draft export
// download) fails. The normalization interceptor must surface an error that
// parseDraftRoomError can decode: backend detail AND code preserved, not
// statusText/generic. This test asserts REQUIRED behavior and is expected to
// FAIL until the fix lands; it prints an AC<n> CHECK sentinel.
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "./core";
import { exportDraftRevision, parseDraftRoomError } from "./draftRoom";

describe("exportDraftRevision blob error decode (issue #516)", () => {
  const originalAdapter = apiClient.defaults.adapter;

  afterEach(() => {
    apiClient.defaults.adapter = originalAdapter;
    vi.unstubAllGlobals();
  });

  it("AC21: preserves the backend detail and code from a JSON Blob error body on a 422", async () => {
    try {
      // core.ts's CSRF request interceptor runs before the adapter for POSTs.
      // Seed the cookie jar so ensureCsrfToken() resolves without a network
      // fetch; stub fetch as a belt-and-braces fallback for the same reason.
      document.cookie = "X-CSRF-Token=test-csrf-token; path=/";
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ csrf_token: "test-csrf-token" }),
      });
      vi.stubGlobal("fetch", fetchMock);

      const envelope = {
        detail: "Export blocked: revision 2 has unresolved fact-check findings.",
        code: "fact_check_not_passed",
        context: { unresolved_findings: 3 },
      };
      const blob = new Blob([JSON.stringify(envelope)], { type: "application/json" });

      // Reject at the axios adapter level so core.ts's real response
      // interceptors (CSRF retry + normalization) stay in the chain.
      apiClient.defaults.adapter = (async () => {
        throw Object.assign(new Error("Request failed with status code 422"), {
          isAxiosError: true,
          config: {
            method: "post",
            url: "/draft-room/drafts/1/revisions/2/export",
            headers: {},
          },
          response: {
            status: 422,
            statusText: "Unprocessable Entity",
            data: blob,
            headers: {},
          },
        });
      }) as unknown as typeof apiClient.defaults.adapter;

      let caught: unknown = null;
      try {
        await exportDraftRevision(1, 2, { format: "md" });
      } catch (error) {
        caught = error;
      }
      expect(caught).toBeInstanceOf(Error);

      const info = parseDraftRoomError(caught);
      // REQUIRED: the backend envelope survives the blob — the detail is the
      // server's reason (not the HTTP statusText) and the code is preserved.
      expect(info.detail).toBe(envelope.detail);
      expect(info.code).toBe(envelope.code);
      expect(info.status).toBe(422);

      console.log("AC21 CHECK: PASS");
    } catch (err) {
      console.log("AC21 CHECK: FAIL");
      throw err;
    }
  });
});
