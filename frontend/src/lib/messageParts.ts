// frontend/src/lib/messageParts.ts
// Typed message-parts model (issue #554, following the AI SDK 5 parts
// pattern): a message is modeled as an ordered list of typed parts instead
// of a growing set of ad-hoc fields. The reasoning display introduced by
// #554 is the first part consumed through this model; text and source part
// shapes are defined alongside it so later migrations are additive cases,
// not another ad-hoc field.
import type { Source } from "@/lib/api";

/** Model reasoning surfaced beside the answer (issue #554). */
export interface ReasoningPart {
  kind: "reasoning";
  text: string;
  /** Wall-clock reasoning span (first to last reasoning delta), ms. */
  durationMs: number;
  /** chars//4 estimate — providers do not report reasoning usage. */
  tokensEstimate: number;
}

/** The answer body. */
export interface TextPart {
  kind: "text";
  text: string;
}

/** A cited retrieval source attached to the message. */
export interface SourcePart {
  kind: "source";
  source: Source;
}

export type MessagePart = ReasoningPart | TextPart | SourcePart;

/** Select the reasoning part from a parts list, if one exists. */
export function getReasoningPart(
  parts: MessagePart[] | undefined | null
): ReasoningPart | undefined {
  if (!parts || parts.length === 0) return undefined;
  return parts.find((part): part is ReasoningPart => part.kind === "reasoning");
}
