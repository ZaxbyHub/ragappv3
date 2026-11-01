import { describe, expect, it } from "vitest";

import { normalizeCanvasDetail } from "./canvas";

describe("issue #773 canvas wire contract", () => {
  it("normalizeCanvasDetail accepts the flat artifact payload the backend emits", () => {
    const payload = {
      artifact_uid: "cav_flat_773",
      session_id: 17,
      message_id: null,
      turn_id: "turn-773",
      kind: "code",
      name: "example.py",
      language: "py",
      current_version_no: 1,
      source_refs: [],
      vault_id: 4,
      created_by: 9,
      created_at: "2026-09-30T00:00:00Z",
      updated_at: "2026-09-30T00:00:00Z",
      current_version: {
        version_no: 1,
        name: null,
        origin: "created",
        model_edit: null,
        content_sha256: "sha-773",
        created_by: 9,
        created_at: "2026-09-30T00:00:00Z",
        content: "print('ok')",
      },
    };

    const result = normalizeCanvasDetail(payload as never);

    expect(result.artifact.artifact_uid).toBe(payload.artifact_uid);
    expect(result.version).toEqual(payload.current_version);
  });
});
