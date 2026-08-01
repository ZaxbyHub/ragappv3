import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockGet, mockPost, mockPatch, mockDelete } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPatch: vi.fn(),
  mockDelete: vi.fn(),
}));

vi.mock("./core", () => ({
  apiClient: {
    get: mockGet,
    post: mockPost,
    patch: mockPatch,
    delete: mockDelete,
  },
  API_BASE_URL: "/api",
}));

import {
  archiveDraft,
  BLOCKING_CLAIM_STATUSES,
  cancelDraftJob,
  compileDraft,
  createDraft,
  createDraftRevision,
  deleteDraft,
  deleteDraftInput,
  draftRoomKeys,
  DRAFT_CLAIM_RESOLUTIONS,
  DRAFT_CLAIM_SEVERITIES,
  DRAFT_CLAIM_SOURCE_RELATIONSHIPS,
  DRAFT_CLAIM_STATUSES,
  DRAFT_CLAIM_TYPES,
  DRAFT_COMPILE_START_STAGES,
  DRAFT_EVIDENCE_SOURCE_KINDS,
  DRAFT_FACT_STATUSES,
  DRAFT_FINDING_CATEGORIES,
  DRAFT_FINDING_SEVERITIES,
  DRAFT_FINDING_STATUSES,
  DRAFT_INPUT_AUTHORITIES,
  DRAFT_INPUT_PARSE_STATUSES,
  DRAFT_INPUT_ROLES,
  DRAFT_JOB_STATUSES,
  DRAFT_JOB_TYPES,
  DRAFT_MODES,
  DRAFT_PROMOTE_SOURCE_TYPES,
  DRAFT_REVISION_SOURCES,
  DRAFT_STAGE_NAMES,
  DRAFT_STAGE_STATUSES,
  DRAFT_STATUSES,
  DRAFT_TIERS,
  DRAFT_VAULT_ACCESS_LEVELS,
  FACT_CURRENT_STATUSES,
  exportDraftRevision,
  getDraft,
  getDraftEventsUrl,
  getDraftInputContent,
  getDraftJob,
  getDraftRevision,
  getDraftRoomCapabilities,
  getDraftStages,
  isDraftRoomErrorCode,
  listDraftClaims,
  listDraftEvidence,
  listDraftFindings,
  listDraftJobs,
  listDraftRevisions,
  listDrafts,
  markDraftRevisionReady,
  parseDraftRoomError,
  promoteDraftSource,
  restoreDraft,
  retryDraftJob,
  setDraftFindingDisposition,
  updateDraft,
  updateDraftInput,
  uploadDraftInput,
  type CompileRequest,
} from "./draftRoom";

beforeEach(() => {
  mockGet.mockReset().mockResolvedValue({ data: {} });
  mockPost.mockReset().mockResolvedValue({ data: {} });
  mockPatch.mockReset().mockResolvedValue({ data: {} });
  mockDelete.mockReset().mockResolvedValue({ data: undefined });
});

// ============================================================================
// Table-driven: every plain endpoint issues the correct HTTP method + URL +
// params/body.
// ============================================================================

interface EndpointCase {
  name: string;
  run: () => Promise<void>;
}

const endpointCases: EndpointCase[] = [
  {
    name: "getDraftRoomCapabilities -> GET /draft-room/capabilities",
    run: async () => {
      await getDraftRoomCapabilities();
      expect(mockGet).toHaveBeenCalledWith("/draft-room/capabilities");
    },
  },
  {
    name: "createDraft -> POST /draft-room/drafts",
    run: async () => {
      const body = { vault_id: 1, title: "t", mode: "compose" as const, brief: {} } as Parameters<
        typeof createDraft
      >[0];
      await createDraft(body);
      expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts", body);
    },
  },
  {
    name: "listDrafts -> GET /draft-room/drafts with default params",
    run: async () => {
      await listDrafts();
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts", { params: {} });
    },
  },
  {
    name: "listDrafts -> GET /draft-room/drafts forwards params",
    run: async () => {
      await listDrafts({ vault_id: 3, status: "ready", page: 2, per_page: 10 });
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts", {
        params: { vault_id: 3, status: "ready", page: 2, per_page: 10 },
      });
    },
  },
  {
    name: "getDraft -> GET /draft-room/drafts/:id",
    run: async () => {
      await getDraft(7);
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7");
    },
  },
  {
    name: "updateDraft -> PATCH /draft-room/drafts/:id",
    run: async () => {
      const body = { lock_version: 2, title: "new" };
      await updateDraft(7, body);
      expect(mockPatch).toHaveBeenCalledWith("/draft-room/drafts/7", body);
    },
  },
  {
    name: "archiveDraft -> POST /draft-room/drafts/:id/archive with lock_version",
    run: async () => {
      await archiveDraft(7, 4);
      expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/7/archive", { lock_version: 4 });
    },
  },
  {
    name: "restoreDraft -> POST /draft-room/drafts/:id/restore with lock_version",
    run: async () => {
      await restoreDraft(7, 5);
      expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/7/restore", { lock_version: 5 });
    },
  },
  {
    name: "deleteDraft -> DELETE /draft-room/drafts/:id",
    run: async () => {
      await deleteDraft(7);
      expect(mockDelete).toHaveBeenCalledWith("/draft-room/drafts/7");
    },
  },
  {
    name: "updateDraftInput -> PATCH /draft-room/drafts/:id/inputs/:inputId",
    run: async () => {
      const body = { role: "reference" as const };
      await updateDraftInput(7, 9, body);
      expect(mockPatch).toHaveBeenCalledWith("/draft-room/drafts/7/inputs/9", body);
    },
  },
  {
    name: "getDraftInputContent -> GET /draft-room/drafts/:id/inputs/:inputId/content",
    run: async () => {
      await getDraftInputContent(7, 9);
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/inputs/9/content");
    },
  },
  {
    name: "deleteDraftInput -> DELETE /draft-room/drafts/:id/inputs/:inputId",
    run: async () => {
      await deleteDraftInput(7, 9);
      expect(mockDelete).toHaveBeenCalledWith("/draft-room/drafts/7/inputs/9");
    },
  },
  {
    name: "listDraftJobs -> GET /draft-room/drafts/:id/jobs with default params",
    run: async () => {
      await listDraftJobs(7);
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/jobs", { params: {} });
    },
  },
  {
    name: "listDraftJobs -> GET /draft-room/drafts/:id/jobs forwards params",
    run: async () => {
      await listDraftJobs(7, { page: 2, per_page: 20 });
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/jobs", {
        params: { page: 2, per_page: 20 },
      });
    },
  },
  {
    name: "getDraftJob -> GET /draft-room/drafts/:id/jobs/:jobId",
    run: async () => {
      await getDraftJob(7, 12);
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/jobs/12");
    },
  },
  {
    name: "getDraftStages -> GET .../jobs/:jobId/stages with default params",
    run: async () => {
      await getDraftStages(7, 12);
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/jobs/12/stages", { params: {} });
    },
  },
  {
    name: "getDraftStages -> GET .../jobs/:jobId/stages forwards include_content",
    run: async () => {
      await getDraftStages(7, 12, { include_content: true, page: 1 });
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/jobs/12/stages", {
        params: { include_content: true, page: 1 },
      });
    },
  },
  {
    name: "cancelDraftJob -> POST .../jobs/:jobId/cancel with no body",
    run: async () => {
      await cancelDraftJob(7, 12);
      expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/7/jobs/12/cancel");
    },
  },
  {
    name: "retryDraftJob -> POST .../jobs/:jobId/retry with empty body by default",
    run: async () => {
      await retryDraftJob(7, 12);
      expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/7/jobs/12/retry", {});
    },
  },
  {
    name: "retryDraftJob -> POST .../jobs/:jobId/retry forwards start_stage",
    run: async () => {
      await retryDraftJob(7, 12, { start_stage: "lint" });
      expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/7/jobs/12/retry", {
        start_stage: "lint",
      });
    },
  },
  {
    name: "listDraftRevisions -> GET .../revisions with default params",
    run: async () => {
      await listDraftRevisions(7);
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/revisions", { params: {} });
    },
  },
  {
    name: "getDraftRevision -> GET .../revisions/:revisionId",
    run: async () => {
      await getDraftRevision(7, 3);
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/revisions/3");
    },
  },
  {
    name: "createDraftRevision -> POST .../revisions",
    run: async () => {
      const body = { base_revision_id: null, lock_version: 1, content_md: "# hi" };
      await createDraftRevision(7, body);
      expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/7/revisions", body);
    },
  },
  {
    name: "markDraftRevisionReady -> POST .../revisions/:revisionId/ready",
    run: async () => {
      const body = { lock_version: 1, acknowledge_source_only: true };
      await markDraftRevisionReady(7, 3, body);
      expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/7/revisions/3/ready", body);
    },
  },
  {
    name: "listDraftEvidence -> GET .../evidence with default params",
    run: async () => {
      await listDraftEvidence(7);
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/evidence", { params: {} });
    },
  },
  {
    name: "listDraftClaims -> GET .../claims with default params",
    run: async () => {
      await listDraftClaims(7);
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/claims", { params: {} });
    },
  },
  {
    name: "listDraftClaims -> GET .../claims forwards status filter",
    run: async () => {
      await listDraftClaims(7, { status: "contradicted" });
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/claims", {
        params: { status: "contradicted" },
      });
    },
  },
  {
    name: "listDraftFindings -> GET .../findings with default params",
    run: async () => {
      await listDraftFindings(7);
      expect(mockGet).toHaveBeenCalledWith("/draft-room/drafts/7/findings", { params: {} });
    },
  },
  {
    name: "setDraftFindingDisposition -> POST .../findings/:findingId/disposition",
    run: async () => {
      const body = { action: "waive" as const, base_revision_id: 3, lock_version: 1, note: "ok" };
      await setDraftFindingDisposition(7, 44, body);
      expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/7/findings/44/disposition", body);
    },
  },
  {
    name: "promoteDraftSource -> POST .../promote",
    run: async () => {
      const body = { source_type: "revision" as const, source_id: 3, title: "Promoted" };
      await promoteDraftSource(7, body);
      expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/7/promote", body);
    },
  },
];

describe("draftRoom API — endpoint dispatch", () => {
  it.each(endpointCases.map((c): [string, EndpointCase["run"]] => [c.name, c.run]))(
    "%s",
    async (_name, run) => {
      await run();
    }
  );
});

// ============================================================================
// uploadDraftInput — multipart precedent (core.ts uploadDocument)
// ============================================================================

describe("uploadDraftInput", () => {
  it("builds FormData with all four fields and the documented axios options", async () => {
    mockPost.mockResolvedValueOnce({ data: { input: {}, job: {} } });
    const file = new File(["hello"], "brief.txt", { type: "text/plain" });
    const onUploadProgress = vi.fn();

    await uploadDraftInput(
      1,
      { file, role: "manuscript", authority: "primary", as_of_date: "2026-01-01" },
      onUploadProgress
    );

    expect(mockPost).toHaveBeenCalledTimes(1);
    const [url, body, config] = mockPost.mock.calls[0] as [
      string,
      FormData,
      { timeout: number; headers: Record<string, string>; onUploadProgress: (e: { loaded: number; total?: number }) => void },
    ];
    expect(url).toBe("/draft-room/drafts/1/inputs");
    expect(body).toBeInstanceOf(FormData);
    expect(body.get("file")).toBe(file);
    expect(body.get("role")).toBe("manuscript");
    expect(body.get("authority")).toBe("primary");
    expect(body.get("as_of_date")).toBe("2026-01-01");
    expect(config.timeout).toBe(0);
    expect(config.headers).toEqual({ "Content-Type": "" });
    expect(typeof config.onUploadProgress).toBe("function");
  });

  it("omits as_of_date when not provided", async () => {
    mockPost.mockResolvedValueOnce({ data: { input: {}, job: {} } });
    const file = new File(["hello"], "brief.txt");

    await uploadDraftInput(1, { file, role: "manuscript", authority: "primary" });

    const [, body] = mockPost.mock.calls[0] as [string, FormData];
    expect(body.get("as_of_date")).toBeNull();
  });

  it("reports upload progress for known and unknown totals", async () => {
    mockPost.mockResolvedValueOnce({ data: { input: {}, job: {} } });
    const file = new File(["hello"], "brief.txt");
    const onUploadProgress = vi.fn();

    await uploadDraftInput(1, { file, role: "manuscript", authority: "primary" }, onUploadProgress);

    const [, , config] = mockPost.mock.calls[0] as [
      string,
      FormData,
      { onUploadProgress: (e: { loaded: number; total?: number }) => void },
    ];
    config.onUploadProgress({ loaded: 50, total: 100 });
    expect(onUploadProgress).toHaveBeenCalledWith(50);

    config.onUploadProgress({ loaded: 50, total: 0 });
    expect(onUploadProgress).toHaveBeenCalledWith(0);
  });
});

// ============================================================================
// compileDraft — Idempotency-Key header
// ============================================================================

describe("compileDraft", () => {
  const body: CompileRequest = { base_revision_id: null, lock_version: 1 };

  it("omits the Idempotency-Key header when no key is supplied", async () => {
    await compileDraft(1, body);
    expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/1/compile", body, undefined);
  });

  it("sets the Idempotency-Key header when a key is supplied", async () => {
    await compileDraft(1, body, "abc-123");
    expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/1/compile", body, {
      headers: { "Idempotency-Key": "abc-123" },
    });
  });
});

// ============================================================================
// getDraftEventsUrl — does not touch apiClient
// ============================================================================

describe("getDraftEventsUrl", () => {
  it("returns the absolute SSE url built from API_BASE_URL", () => {
    expect(getDraftEventsUrl(42)).toBe("/api/draft-room/drafts/42/events");
    expect(mockGet).not.toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
  });
});

// ============================================================================
// exportDraftRevision — query params only, blob response, header parsing
// ============================================================================

describe("exportDraftRevision", () => {
  it("sends query params (not a body), requests a blob, and parses a quoted filename + all three headers", async () => {
    const blob = new Blob(["# hi"], { type: "text/markdown" });
    mockPost.mockResolvedValueOnce({
      data: blob,
      headers: {
        "content-disposition": 'attachment; filename="draft-rev1.md"',
        "x-draft-fact-status": "passed",
        "x-draft-approval-status": "ready",
        "x-draft-content-sha256": "deadbeef",
      },
    });

    const result = await exportDraftRevision(1, 2, { format: "md", acknowledge_not_fact_checked: true });

    expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/1/revisions/2/export", undefined, {
      params: { format: "md", acknowledge_not_fact_checked: true },
      responseType: "blob",
    });
    expect(result.blob).toBe(blob);
    expect(result.filename).toBe("draft-rev1.md");
    expect(result.factStatus).toBe("passed");
    expect(result.approvalStatus).toBe("ready");
    expect(result.contentSha256).toBe("deadbeef");
  });

  it("parses an unquoted filename and reads headers case-insensitively", async () => {
    mockPost.mockResolvedValueOnce({
      data: new Blob(["x"]),
      headers: {
        "Content-Disposition": "attachment; filename=draft-rev1-REVIEW.md",
        "X-Draft-Fact-Status": "findings",
        "X-Draft-Approval-Status": "not_ready",
        "X-Draft-Content-Sha256": "cafebabe",
      },
    });

    const result = await exportDraftRevision(1, 2);

    expect(result.filename).toBe("draft-rev1-REVIEW.md");
    expect(result.factStatus).toBe("findings");
    expect(result.approvalStatus).toBe("not_ready");
    expect(result.contentSha256).toBe("cafebabe");
  });

  it("uses default params and falls back to a default filename / empty strings when headers are missing", async () => {
    mockPost.mockResolvedValueOnce({ data: new Blob(["x"]), headers: {} });

    const result = await exportDraftRevision(1, 2);

    expect(mockPost).toHaveBeenCalledWith("/draft-room/drafts/1/revisions/2/export", undefined, {
      params: {},
      responseType: "blob",
    });
    expect(result.filename).toBe("draft.md");
    expect(result.factStatus).toBe("");
    expect(result.approvalStatus).toBe("");
    expect(result.contentSha256).toBe("");
  });
});

// ============================================================================
// parseDraftRoomError / isDraftRoomErrorCode
// ============================================================================

describe("parseDraftRoomError", () => {
  it("extracts detail/code/context/status from a normalized core.ts error", () => {
    const error = Object.assign(new Error("draft was modified by another request; reload and retry"), {
      status: 409,
      originalError: {
        response: {
          data: {
            detail: "draft was modified by another request; reload and retry",
            code: "conflict",
            context: { existing_input_id: 123 },
          },
        },
      },
    });

    expect(parseDraftRoomError(error)).toEqual({
      detail: "draft was modified by another request; reload and retry",
      code: "conflict",
      context: { existing_input_id: 123 },
      status: 409,
    });
  });

  it("falls back safely for a bare Error with no originalError", () => {
    expect(parseDraftRoomError(new Error("boom"))).toEqual({
      detail: "boom",
      code: "unknown",
      context: {},
    });
  });

  it("falls back safely for a plain string", () => {
    expect(parseDraftRoomError("oops")).toEqual({
      detail: "An unexpected error occurred",
      code: "unknown",
      context: {},
    });
  });

  it("falls back safely for null", () => {
    expect(parseDraftRoomError(null)).toEqual({
      detail: "An unexpected error occurred",
      code: "unknown",
      context: {},
    });
  });

  it("falls back safely for an object with no response data", () => {
    const error = { status: 500, originalError: {} };
    expect(parseDraftRoomError(error)).toEqual({
      detail: "An unexpected error occurred",
      code: "unknown",
      context: {},
      status: 500,
    });
  });
});

describe("isDraftRoomErrorCode", () => {
  it("matches the extracted code and rejects other codes", () => {
    const error = Object.assign(new Error("x"), {
      originalError: { response: { data: { code: "conflict" } } },
    });
    expect(isDraftRoomErrorCode(error, "conflict")).toBe(true);
    expect(isDraftRoomErrorCode(error, "invalid_state")).toBe(false);
  });
});

// ============================================================================
// draftRoomKeys — stable, distinct, nested query keys
// ============================================================================

describe("draftRoomKeys", () => {
  it("produces stable, distinct, nested keys", () => {
    expect(draftRoomKeys.all).toEqual(["draft-room"]);
    expect(draftRoomKeys.capabilities()).toEqual(["draft-room", "capabilities"]);
    expect(draftRoomKeys.lists()).toEqual(["draft-room", "drafts"]);
    expect(draftRoomKeys.list({ page: 1 })).toEqual(["draft-room", "drafts", { page: 1 }]);
    expect(draftRoomKeys.detail(5)).toEqual(["draft-room", "draft", 5]);
    expect(draftRoomKeys.inputs(5)).toEqual(["draft-room", "draft", 5, "inputs"]);
    expect(draftRoomKeys.jobs(5)).toEqual(["draft-room", "draft", 5, "jobs", {}]);
    expect(draftRoomKeys.jobs(5, { page: 2 })).toEqual(["draft-room", "draft", 5, "jobs", { page: 2 }]);
    expect(draftRoomKeys.job(5, 9)).toEqual(["draft-room", "draft", 5, "job", 9]);
    expect(draftRoomKeys.stages(5, 9, true)).toEqual(["draft-room", "draft", 5, "job", 9, "stages", true]);
    expect(draftRoomKeys.stages(5, 9)).toEqual(["draft-room", "draft", 5, "job", 9, "stages", false]);
    expect(draftRoomKeys.revisions(5)).toEqual(["draft-room", "draft", 5, "revisions", {}]);
    expect(draftRoomKeys.revision(5, 3)).toEqual(["draft-room", "draft", 5, "revision", 3]);
    expect(draftRoomKeys.evidence(5)).toEqual(["draft-room", "draft", 5, "evidence", {}]);
    expect(draftRoomKeys.claims(5)).toEqual(["draft-room", "draft", 5, "claims", {}]);
    expect(draftRoomKeys.findings(5)).toEqual(["draft-room", "draft", 5, "findings", {}]);
    expect(draftRoomKeys.promotions(5)).toEqual(["draft-room", "draft", 5, "promotions"]);

    // Distinct resources for the same draft id must not collide.
    expect(draftRoomKeys.inputs(5)).not.toEqual(draftRoomKeys.promotions(5));
    expect(draftRoomKeys.detail(5)).not.toEqual(draftRoomKeys.detail(6));
  });
});

// ============================================================================
// Enum tuples — guard against silent drift from the backend contract
// ============================================================================

const enumCases: Array<[string, readonly string[], string[]]> = [
  ["DRAFT_STATUSES", DRAFT_STATUSES, ["draft", "queued", "running", "needs_review", "ready", "failed", "cancelled", "archived"]],
  ["DRAFT_JOB_STATUSES", DRAFT_JOB_STATUSES, ["pending", "running", "completed", "failed", "cancelled"]],
  ["DRAFT_INPUT_PARSE_STATUSES", DRAFT_INPUT_PARSE_STATUSES, ["pending", "parsing", "ready", "failed", "cancelled"]],
  ["DRAFT_MODES", DRAFT_MODES, ["rewrite", "compose"]],
  ["DRAFT_TIERS", DRAFT_TIERS, ["standard", "high_stakes", "sensitive"]],
  ["DRAFT_INPUT_ROLES", DRAFT_INPUT_ROLES, ["manuscript", "reference", "style", "background", "challenge"]],
  [
    "DRAFT_INPUT_AUTHORITIES",
    DRAFT_INPUT_AUTHORITIES,
    ["primary", "official", "secondary", "user_asserted", "unknown"],
  ],
  ["DRAFT_JOB_TYPES", DRAFT_JOB_TYPES, ["parse_input", "compile"]],
  ["DRAFT_REVISION_SOURCES", DRAFT_REVISION_SOURCES, ["pipeline", "manual"]],
  [
    "DRAFT_STAGE_NAMES",
    DRAFT_STAGE_NAMES,
    ["intake", "research", "outline", "draft", "lint", "copy", "standards", "fact", "assemble"],
  ],
  [
    "DRAFT_STAGE_STATUSES",
    DRAFT_STAGE_STATUSES,
    ["pending", "running", "completed", "failed", "skipped", "cancelled"],
  ],
  ["DRAFT_EVIDENCE_SOURCE_KINDS", DRAFT_EVIDENCE_SOURCE_KINDS, ["draft_input", "document", "wiki", "kms"]],
  ["DRAFT_CLAIM_TYPES", DRAFT_CLAIM_TYPES, ["factual", "quote", "opinion"]],
  [
    "DRAFT_CLAIM_STATUSES",
    DRAFT_CLAIM_STATUSES,
    ["supported", "contradicted", "ambiguous", "stale", "unsupported", "opinion"],
  ],
  ["DRAFT_CLAIM_SEVERITIES", DRAFT_CLAIM_SEVERITIES, ["info", "warning", "blocker"]],
  [
    "DRAFT_CLAIM_RESOLUTIONS",
    DRAFT_CLAIM_RESOLUTIONS,
    ["open", "resolved_by_revision", "accepted", "waived"],
  ],
  [
    "DRAFT_CLAIM_SOURCE_RELATIONSHIPS",
    DRAFT_CLAIM_SOURCE_RELATIONSHIPS,
    ["supports", "contradicts", "context"],
  ],
  [
    "DRAFT_FINDING_CATEGORIES",
    DRAFT_FINDING_CATEGORIES,
    ["boilerplate", "style", "preservation", "factuality", "quote", "conflict", "security", "operational"],
  ],
  ["DRAFT_FINDING_SEVERITIES", DRAFT_FINDING_SEVERITIES, ["info", "warning", "blocker"]],
  [
    "DRAFT_FINDING_STATUSES",
    DRAFT_FINDING_STATUSES,
    ["open", "applied", "dismissed", "waived", "resolved_by_revision"],
  ],
  ["DRAFT_VAULT_ACCESS_LEVELS", DRAFT_VAULT_ACCESS_LEVELS, ["write", "read", "revoked"]],
  ["DRAFT_FACT_STATUSES", DRAFT_FACT_STATUSES, ["not_run", "running", "passed", "findings", "invalidated"]],
  [
    "DRAFT_COMPILE_START_STAGES",
    DRAFT_COMPILE_START_STAGES,
    ["research", "outline", "draft", "lint", "copy", "standards", "fact"],
  ],
  ["DRAFT_PROMOTE_SOURCE_TYPES", DRAFT_PROMOTE_SOURCE_TYPES, ["input", "revision"]],
  ["BLOCKING_CLAIM_STATUSES", BLOCKING_CLAIM_STATUSES, ["contradicted", "unsupported", "ambiguous", "stale"]],
  ["FACT_CURRENT_STATUSES", FACT_CURRENT_STATUSES, ["passed", "findings"]],
];

describe("enum tuples match the documented backend contract exactly", () => {
  it.each(enumCases)("%s", (_name, actual, expected) => {
    expect(actual).toEqual(expected);
  });
});
