import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockGet, mockPost } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
}));

vi.mock("./core", () => ({
  apiClient: {
    get: mockGet,
    post: mockPost,
  },
}));

import {
  canvasKeys,
  createCanvasArtifact,
  downloadCanvasVersion,
  editCanvasRange,
  exportCanvasManifest,
  getCanvasArtifact,
  getCanvasCapabilities,
  getCanvasErrorDetail,
  getCanvasErrorStatus,
  getCanvasVersion,
  isCanvasVersionConflict,
  listCanvasVersions,
  listSessionArtifacts,
  mapFenceLanguageToExtension,
  normalizeCanvasDetail,
  restoreCanvasVersion,
  saveCanvasVersion,
} from "./canvas";

/** Mirrors the normalized error shape produced by core.ts's response
 * interceptor: a plain Error with `.status` and `.originalError`. */
function makeApiError(status: number, detail: string): Error {
  const error = new Error(detail);
  (error as unknown as { status?: number }).status = status;
  (error as unknown as { originalError?: unknown }).originalError = {
    response: { status, data: { detail } },
  };
  return error;
}

beforeEach(() => {
  // Detail GETs must always resolve a version-bearing payload; mutations
  // return bare versions.
  mockGet.mockReset().mockResolvedValue({
    data: { artifact: RAW_ARTIFACT, current_version: RAW_VERSION },
  });
  mockPost.mockReset().mockResolvedValue({ data: RAW_VERSION });
});

// ============================================================================
// Table-driven: every plain endpoint issues the correct HTTP method + URL +
// params/body.
// ============================================================================

describe("canvas endpoint request shapes", () => {
  it("getCanvasCapabilities -> GET /canvas/capabilities", async () => {
    await getCanvasCapabilities();
    expect(mockGet).toHaveBeenCalledWith("/canvas/capabilities");
  });

  it("createCanvasArtifact -> POST /chat/sessions/{id}/artifacts with body", async () => {
    const payload = {
      kind: "code" as const,
      name: "demo.py",
      language: "python",
      content: "print('hi')",
      message_id: 42,
      source_refs: [{ source_id: "s1", title: "a.pdf" }],
    };
    mockPost.mockResolvedValueOnce({ data: { artifact: RAW_ARTIFACT, current_version: RAW_VERSION } });
    await createCanvasArtifact(7, payload);
    expect(mockPost).toHaveBeenCalledWith("/chat/sessions/7/artifacts", payload);
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it("listSessionArtifacts -> GET /chat/sessions/{id}/artifacts", async () => {
    await listSessionArtifacts(7);
    expect(mockGet).toHaveBeenCalledWith("/chat/sessions/7/artifacts");
  });

  it("getCanvasArtifact -> GET /canvas/artifacts/{uid}", async () => {
    await getCanvasArtifact("cav_test1");
    expect(mockGet).toHaveBeenCalledWith("/canvas/artifacts/cav_test1");
  });

  it("getCanvasArtifact URL-encodes the uid", async () => {
    await getCanvasArtifact("cav_a/b c");
    expect(mockGet).toHaveBeenCalledWith("/canvas/artifacts/cav_a%2Fb%20c");
  });

  it("listCanvasVersions -> GET /canvas/artifacts/{uid}/versions", async () => {
    await listCanvasVersions("cav_test1");
    expect(mockGet).toHaveBeenCalledWith("/canvas/artifacts/cav_test1/versions");
  });

  it("saveCanvasVersion -> POST /canvas/artifacts/{uid}/versions with base_version_no + force", async () => {
    await saveCanvasVersion("cav_test1", {
      content: "new content",
      name: "Post fix",
      base_version_no: 2,
      force: true,
    });
    expect(mockPost).toHaveBeenCalledWith("/canvas/artifacts/cav_test1/versions", {
      content: "new content",
      name: "Post fix",
      base_version_no: 2,
      force: true,
    });
  });

  it("restoreCanvasVersion -> POST /canvas/artifacts/{uid}/restore", async () => {
    await restoreCanvasVersion("cav_test1", { version_no: 1, base_version_no: 3 });
    expect(mockPost).toHaveBeenCalledWith("/canvas/artifacts/cav_test1/restore", {
      version_no: 1,
      base_version_no: 3,
    });
  });

  it("editCanvasRange -> POST /canvas/artifacts/{uid}/edit-range", async () => {
    await editCanvasRange("cav_test1", {
      start_line: 2,
      end_line: 4,
      instruction: "Add error handling",
      base_version_no: 2,
    });
    expect(mockPost).toHaveBeenCalledWith(
      "/canvas/artifacts/cav_test1/edit-range",
      {
        start_line: 2,
        end_line: 4,
        instruction: "Add error handling",
        base_version_no: 2,
      },
      { signal: undefined }
    );
  });

  it("exportCanvasManifest -> GET /canvas/artifacts/{uid}/export?version_no=N", async () => {
    await exportCanvasManifest("cav_test1", 2);
    expect(mockGet).toHaveBeenCalledWith("/canvas/artifacts/cav_test1/export", {
      params: { version_no: 2 },
    });
  });

  it("saveCanvasVersion returns the appended version object directly", async () => {
    const version = {
      version_no: 3,
      name: null,
      origin: "user_edit" as const,
      model_edit: null,
      content_sha256: "sha-3",
      created_at: "2026-01-03T00:00:00Z",
      content: "new",
    };
    mockPost.mockResolvedValueOnce({ data: version });
    await expect(
      saveCanvasVersion("cav_test1", { content: "new", base_version_no: 2, force: false })
    ).resolves.toEqual(version);
  });
});

// ============================================================================
// Detail payload normalization (backend embeds the version as current_version)
// ============================================================================

const RAW_ARTIFACT = {
  artifact_uid: "cav_test1",
  session_id: 7,
  message_id: 42,
  turn_id: "turn-1",
  kind: "code",
  name: "demo.py",
  language: "py",
  current_version_no: 2,
  source_refs: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
  // Superset fields the backend adds — tolerated.
  vault_id: 1,
  created_by: 3,
};

const RAW_VERSION = {
  version_no: 2,
  name: null,
  origin: "user_edit",
  model_edit: null,
  content_sha256: "sha-2",
  created_at: "2026-01-02T00:00:00Z",
  content: "body",
};

describe("canvas detail payload normalization", () => {
  it("getCanvasArtifact normalizes the emitted top-level current_version shape", async () => {
    mockGet.mockResolvedValueOnce({
      data: { artifact: RAW_ARTIFACT, current_version: RAW_VERSION },
    });
    const result = await getCanvasArtifact("cav_test1");
    expect(result.version).toEqual(RAW_VERSION);
    expect(result.artifact.artifact_uid).toBe("cav_test1");
  });

  it("getCanvasVersion rejects a payload without current_version (no speculative shapes)", async () => {
    mockGet.mockResolvedValueOnce({ data: { artifact: RAW_ARTIFACT, version: RAW_VERSION } });
    await expect(getCanvasVersion("cav_test1", 2)).rejects.toThrow(/missing its version/);
  });

  it("createCanvasArtifact normalizes a top-level current_version key", async () => {
    mockPost.mockResolvedValueOnce({
      data: { artifact: RAW_ARTIFACT, current_version: RAW_VERSION },
    });
    const result = await createCanvasArtifact(7, {
      kind: "code",
      name: "demo.py",
      content: "body",
    });
    expect(result.version).toEqual(RAW_VERSION);
  });

  it("normalizeCanvasDetail throws on a payload with no version anywhere", () => {
    expect(() => normalizeCanvasDetail({ artifact: RAW_ARTIFACT })).toThrow(/missing its version/);
  });
});

// ============================================================================
// Fence language -> backend extension-key mapping (contract delta #2)
// ============================================================================

describe("mapFenceLanguageToExtension", () => {
  it("maps common fence languages to file extensions", () => {
    expect(mapFenceLanguageToExtension("python")).toBe("py");
    expect(mapFenceLanguageToExtension("TypeScript")).toBe("ts");
    expect(mapFenceLanguageToExtension("javascript")).toBe("js");
    expect(mapFenceLanguageToExtension("tsx")).toBe("tsx");
    expect(mapFenceLanguageToExtension("bash")).toBe("sh");
    expect(mapFenceLanguageToExtension("shell")).toBe("sh");
    expect(mapFenceLanguageToExtension("c++")).toBe("cpp");
    expect(mapFenceLanguageToExtension("c#")).toBe("cs");
    expect(mapFenceLanguageToExtension("golang")).toBe("go");
    expect(mapFenceLanguageToExtension("rust")).toBe("rs");
    expect(mapFenceLanguageToExtension("markdown")).toBe("md");
    expect(mapFenceLanguageToExtension("yaml")).toBe("yaml");
  });

  it("passes short unknown languages through lowercased and drops anything else", () => {
    expect(mapFenceLanguageToExtension("Lua")).toBe("lua");
    expect(mapFenceLanguageToExtension("")).toBeNull();
    expect(mapFenceLanguageToExtension("not a real language!!")).toBeNull();
  });
});

// ============================================================================
// Blob download
// ============================================================================

describe("downloadCanvasVersion", () => {
  it("fetches blob bytes and parses the filename from Content-Disposition", async () => {
    const blob = new Blob(["def hello():\n    pass\n"]);
    mockGet.mockResolvedValueOnce({
      data: blob,
      headers: { "content-disposition": 'attachment; filename="demo.py"' },
    });
    const result = await downloadCanvasVersion("cav_test1", 2);

    expect(mockGet).toHaveBeenCalledWith("/canvas/artifacts/cav_test1/versions/2/download", {
      responseType: "blob",
    });
    expect(result.blob).toBe(blob);
    expect(result.filename).toBe("demo.py");
    expect(await result.blob.text()).toBe("def hello():\n    pass\n");
  });

  it("falls back to a default filename without a Content-Disposition header", async () => {
    mockGet.mockResolvedValueOnce({ data: new Blob(["x"]), headers: {} });
    const result = await downloadCanvasVersion("cav_test1", 2);
    expect(result.filename).toBe("canvas.txt");
  });

  it("reads a Blob error body so the backend detail code survives", async () => {
    const blobBody = new Blob([JSON.stringify({ detail: "canvas_not_found" })]);
    const error = new Error("An unexpected error occurred");
    (error as unknown as { status?: number }).status = 404;
    (error as unknown as { originalError?: unknown }).originalError = {
      response: { status: 404, data: blobBody },
    };
    mockGet.mockRejectedValueOnce(error);

    await expect(downloadCanvasVersion("cav_test1", 2)).rejects.toMatchObject({
      message: "canvas_not_found",
    });
  });

  it("rethrows non-JSON blob error bodies unchanged", async () => {
    const blobBody = new Blob(["<html>oops</html>"]);
    const error = new Error("Server Error");
    (error as unknown as { status?: number }).status = 500;
    (error as unknown as { originalError?: unknown }).originalError = {
      response: { status: 500, data: blobBody },
    };
    mockGet.mockRejectedValueOnce(error);

    await expect(downloadCanvasVersion("cav_test1", 2)).rejects.toBe(error);
  });
});

// ============================================================================
// 409 conflict mapping + error helpers + query keys
// ============================================================================

describe("canvas error helpers", () => {
  it("detects the 409 version conflict", () => {
    expect(isCanvasVersionConflict(makeApiError(409, "canvas_version_conflict"))).toBe(true);
  });

  it("does not treat 422 or unknown shapes as a conflict", () => {
    expect(isCanvasVersionConflict(makeApiError(422, "canvas_invalid_range"))).toBe(false);
    expect(isCanvasVersionConflict(new Error("network"))).toBe(false);
  });

  it("extracts status from the normalized error", () => {
    expect(getCanvasErrorStatus(makeApiError(409, "canvas_version_conflict"))).toBe(409);
    expect(getCanvasErrorStatus(new Error("no status"))).toBeUndefined();
  });

  it("extracts the backend detail with message fallback", () => {
    expect(getCanvasErrorDetail(makeApiError(502, "canvas_model_unavailable"))).toBe(
      "canvas_model_unavailable"
    );
    expect(getCanvasErrorDetail(new Error("plain message"))).toBe("plain message");
    expect(getCanvasErrorDetail("not an object")).toBe("An unexpected error occurred");
  });
});

describe("canvasKeys", () => {
  it("nests versions under the artifact key so one invalidation refreshes both", () => {
    expect(canvasKeys.capabilities()).toEqual(["canvas", "capabilities"]);
    expect(canvasKeys.artifact("cav_1")).toEqual(["canvas", "artifact", "cav_1"]);
    expect(canvasKeys.versions("cav_1")).toEqual(["canvas", "artifact", "cav_1", "versions"]);
    expect(canvasKeys.version("cav_1", 2)).toEqual([
      "canvas",
      "artifact",
      "cav_1",
      "versions",
      2,
    ]);
  });
});
