// Regression check for issue #494 AC26 / API-006: core.ts's apiClient
// response interceptor extracts `message = data?.detail || ...`. FastAPI
// validation failures (422) send `detail` as an ARRAY of per-field error
// objects; the array coerces to "[object Object],[object Object]" in
// Error.message, hiding which fields failed and why.
//
// This check asserts REQUIRED behavior that does not exist at the pre-fix
// base (a543361) and is expected to FAIL there; it prints an "AC26 CHECK:
// FAIL" sentinel immediately before the discriminating assertion. Mirrors
// core.issue532blobcap.test.ts: the axios adapter is stubbed to reject
// with an axios-shaped error so the REAL interceptor chain installed on
// the singleton apiClient runs.
import { afterEach, describe, expect, it } from "vitest";

import { apiClient } from "./core";

describe("FastAPI 422 detail-array error normalization (issue #494)", () => {
  const originalAdapter = apiClient.defaults.adapter;

  afterEach(() => {
    apiClient.defaults.adapter = originalAdapter;
  });

  it("AC26: a 422 whose detail is an ARRAY yields a message naming every field and explanation — never '[object Object]'", async () => {
    apiClient.defaults.adapter = (async () => {
      throw Object.assign(new Error("Request failed with status code 422"), {
        isAxiosError: true,
        config: {
          method: "get",
          url: "/settings",
          headers: {},
        },
        response: {
          status: 422,
          statusText: "Unprocessable Entity",
          data: {
            detail: [
              {
                loc: ["body", "chunk_size_chars"],
                msg: "Input should be greater than 0",
                type: "greater_than",
              },
              {
                loc: ["body", "retrieval_window"],
                msg: "Input should be between 0 and 3",
                type: "value_error",
              },
            ],
          },
          headers: {},
        },
      });
    }) as unknown as typeof apiClient.defaults.adapter;

    // GET keeps the CSRF request interceptor inert, so no network stub is
    // needed; the installed response interceptor chain does the work.
    const caught = await apiClient.get("/settings").then(
      () => {
        throw new Error("expected the 422 request to reject");
      },
      (error: unknown) => error,
    );

    console.log("AC26 CHECK: FAIL");

    expect(caught).toBeInstanceOf(Error);
    const message = (caught as Error).message;
    // Both failing field names surface...
    expect(message).toContain("chunk_size_chars");
    expect(message).toContain("retrieval_window");
    // ...and both explanations.
    expect(message).toContain("Input should be greater than 0");
    expect(message).toContain("Input should be between 0 and 3");
    // The coerced-array tell must be gone.
    expect(message).not.toContain("[object Object]");
    // Status stays preserved for downstream status-code branching.
    expect((caught as { status?: number }).status).toBe(422);
  });
});
