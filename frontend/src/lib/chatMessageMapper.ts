import type { ChatSessionMessage } from "@/lib/api";
import type { Message } from "@/stores/useChatStore";

/**
 * THE canonical API-to-store message mapper (issue #507).
 *
 * Every load path — ordinary session load (ChatShell) and fork (TranscriptPane)
 * — must use this single mapper so no field is silently dropped (UI-039: the
 * fork path used to lose kms_refs and mode) and the DEEP-D-01 assessment
 * fields round-trip through save → load → fork.
 *
 * This is also the single place where the backend's nullable ``status`` is
 * normalized: legacy rows (status NULL) render as "complete" — we never invent
 * evidence, we just apply the documented default.
 */
export function mapSessionMessage(m: ChatSessionMessage): Message {
  return {
    id: m.id.toString(),
    role: m.role as "user" | "assistant",
    content: m.content,
    sources: m.sources ?? undefined,
    memoriesUsed: m.memories ?? undefined,
    wikiRefs: m.wiki_refs ?? undefined,
    kmsRefs: m.kms_refs ?? undefined,
    mode: m.mode ?? undefined,
    citationConfidence: m.citation_confidence ?? undefined,
    unverifiableClaims: m.unverifiable_claims ?? undefined,
    turnId: m.turn_id ?? undefined,
    status: m.status ?? "complete",
    seq: m.seq ?? null,
    created_at: m.created_at,
    feedback: m.feedback ?? null,
  };
}
