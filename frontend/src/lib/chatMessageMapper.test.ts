import { describe, expect, it } from "vitest";
import { mapSessionMessage } from "./chatMessageMapper";
import type { ChatSessionMessage } from "@/lib/api";
import type { UsedMemory, WikiReference, KMSReference, Source } from "@/lib/api";

// A fully-populated API row: every snake_case field the mapper must carry
// over (issue #507 / UI-039 — the fork path used to lose kms_refs and mode).
const fullRow: ChatSessionMessage = {
  id: 42,
  role: "assistant",
  content: "Answer [S1] [W1] [K1] [M1]",
  sources: [
    { id: "s1", filename: "doc.pdf", snippet: "evidence" } as Source,
  ],
  memories: [
    { id: "7", memory_label: "M1", content: "User likes lists." },
  ] satisfies UsedMemory[],
  wiki_refs: [
    {
      wiki_label: "W1",
      page_id: 3,
      claim_id: null,
      title: "Wiki page",
      slug: "wiki-page",
      page_type: "note",
      claim_text: null,
      excerpt: "excerpt",
      confidence: 0.8,
      status: "verified",
      page_status: "completed",
      claim_status: null,
    } satisfies WikiReference,
  ],
  kms_refs: [
    {
      kms_label: "K1",
      entry_id: 11,
      slug: "kms-entry",
      title: "KMS entry",
      summary: "summary",
      excerpt: "excerpt",
      tags: ["tag"],
      status: "active",
      source_type: "doc",
      file_id: null,
      score: 0.42,
      score_type: "distance",
    } satisfies KMSReference,
  ],
  created_at: "2026-05-12T00:00:00Z",
  feedback: "up",
  mode: "thinking",
  seq: 4,
  turn_id: "turn-abc-123",
  status: "complete",
  citation_confidence: { S1: 0.93 },
  unverifiable_claims: ["claim A", "claim B"],
};

describe("mapSessionMessage (issue #507)", () => {
  it("maps every field snake→camel without dropping any", () => {
    const mapped = mapSessionMessage(fullRow);

    expect(mapped).toEqual({
      id: "42",
      role: "assistant",
      content: "Answer [S1] [W1] [K1] [M1]",
      sources: fullRow.sources,
      memoriesUsed: fullRow.memories,
      wikiRefs: fullRow.wiki_refs,
      kmsRefs: fullRow.kms_refs,
      mode: "thinking",
      citationConfidence: { S1: 0.93 },
      unverifiableClaims: ["claim A", "claim B"],
      turnId: "turn-abc-123",
      status: "complete",
      seq: 4,
      created_at: "2026-05-12T00:00:00Z",
      feedback: "up",
    });
    // The durable per-session order rides along on the store Message (issue
    // #507 / PRR-020 — revision anchors are computed from it).
    expect(mapped.seq).toBe(4);
  });

  it("normalizes status null → \"complete\" (legacy rows)", () => {
    const legacy = mapSessionMessage({ ...fullRow, status: null, seq: null });
    expect(legacy.status).toBe("complete");
    // Legacy rows with an explicit NULL seq map to seq null (no durable order).
    expect(legacy.seq).toBeNull();

    // A missing status key (older payloads) defaults the same way.
    const omitted = { ...fullRow } as Partial<ChatSessionMessage>;
    delete omitted.status;
    expect(mapSessionMessage(omitted as ChatSessionMessage).status).toBe("complete");
  });

  it("maps a row MISSING the seq key to seq null (not undefined)", () => {
    const noSeqKey = { ...fullRow } as Partial<ChatSessionMessage>;
    delete noSeqKey.seq;
    const mapped = mapSessionMessage(noSeqKey as ChatSessionMessage);
    expect(mapped.seq).toBeNull();
    expect("seq" in mapped).toBe(true);
  });

  it("preserves \"interrupted\" when set", () => {
    const interrupted = mapSessionMessage({ ...fullRow, status: "interrupted" });
    expect(interrupted.status).toBe("interrupted");
  });

  it("maps null optional fields to undefined (not null) on the store Message", () => {
    const legacyRow: ChatSessionMessage = {
      ...fullRow,
      sources: null,
      memories: null,
      wiki_refs: null,
      kms_refs: null,
      mode: null,
      turn_id: null,
      status: null,
      citation_confidence: null,
      unverifiable_claims: null,
      feedback: null,
      seq: null,
    };
    const mapped = mapSessionMessage(legacyRow);
    expect(mapped.sources).toBeUndefined();
    expect(mapped.memoriesUsed).toBeUndefined();
    expect(mapped.wikiRefs).toBeUndefined();
    expect(mapped.kmsRefs).toBeUndefined();
    expect(mapped.mode).toBeUndefined();
    expect(mapped.turnId).toBeUndefined();
    expect(mapped.citationConfidence).toBeUndefined();
    expect(mapped.unverifiableClaims).toBeUndefined();
    expect(mapped.feedback).toBeNull();
    expect(mapped.seq).toBeNull();
  });
});
