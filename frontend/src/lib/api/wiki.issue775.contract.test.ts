/**
 * Candidate coverage for wiki.ts request forwarding.
 *
 * wiki.ts does not own UI retry/cancellation state. It owns the endpoint,
 * vault scope, and response/error propagation; consumer ownership is covered
 * separately by the MaintenanceSettings candidate.
 */
import { describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("./core", () => ({
  apiClient: apiMocks,
}));

import { cancelWikiJob, listWikiJobs, retryWikiJob } from "./wiki";

describe("wiki.ts job request contracts", () => {
  it("forwards the vault scope and backend limit of 10 to listWikiJobs", async () => {
    apiMocks.get.mockResolvedValueOnce({ data: { jobs: [] } });

    await expect(listWikiJobs({ vault_id: 7, limit: 10 })).resolves.toEqual({ jobs: [] });

    expect(apiMocks.get).toHaveBeenCalledWith("/wiki/jobs", {
      params: { vault_id: 7, limit: 10 },
    });
  });

  it("keeps retry and cancel endpoint calls vault-scoped and preserves classified errors", async () => {
    const classifiedError = Object.assign(new Error("backend classification sentinel"), {
      response: { status: 409 },
    });
    apiMocks.post.mockRejectedValueOnce(classifiedError);

    await expect(retryWikiJob(42, 7)).rejects.toBe(classifiedError);
    expect(apiMocks.post).toHaveBeenNthCalledWith(1, "/wiki/jobs/42/retry", null, {
      params: { vault_id: 7 },
    });

    apiMocks.post.mockResolvedValueOnce({ data: { job_id: 42, status: "cancelled" } });
    await expect(cancelWikiJob(42, 7)).resolves.toEqual({ job_id: 42, status: "cancelled" });
    expect(apiMocks.post).toHaveBeenNthCalledWith(2, "/wiki/jobs/42/cancel", null, {
      params: { vault_id: 7 },
    });
  });
});
